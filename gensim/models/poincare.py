#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Author: Jayant Jain <jayantjain1992@gmail.com>
# Copyright (C) 2017 Radim Rehurek <me@radimrehurek.com>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html


"""
Python implementation of Poincare Embeddings, an embedding to capture hierarchical information,
described in [1]

This module allows training a Poincare Embedding from a training file containing relations from
a transitive closure.


.. [1] https://arxiv.org/pdf/1705.08039.pdf

"""


import csv
import itertools
import logging
import os
import random
import time

from autograd import numpy as np, grad
from collections import defaultdict, Counter
from numpy import random as np_random
from smart_open import smart_open

from gensim import utils
from gensim.models.keyedvectors import KeyedVectors, Vocab
from gensim.models.word2vec import Word2Vec

logger = logging.getLogger(__name__)


class PoincareModel(utils.SaveLoad):
    """
    Class for training, using and evaluating Poincare Embeddings described in https://arxiv.org/pdf/1705.08039.pdf

    The model can be stored/loaded via its `save()` and `load()` methods, or stored/loaded in the word2vec
    format via `wv.save_word2vec_format()` and `KeyedVectors.load_word2vec_format()`.

    Note that training cannot be resumed from a model loaded via `load_word2vec_format`, if you wish to train further,
    use `save()` and `load()` methods instead.

    """
    def __init__(
        self, train_data, size=50, alpha=0.1, negative=10, iter=50,
        workers=1, epsilon=1e-5, burn_in=10, burn_in_alpha=0.01, seed=0):
        """
        Initialize and train a Poincare embedding model from an iterable of transitive closure relations.

        Parameters
        ----------
        train_file : iterable
            iterable of hypernym pairs, e.g. a list of tuples, or a PoincareData instance streaming from a file.
        size : int, optional
            Number of dimensions of the trained model, defaults to 50.
        alpha : float, optional
            learning rate for training, defaults to 0.1.
        negative : int, optional
            Number of negative samples to use, defaults to 10.
        iter : int, optional
            Number of iterations (epochs) over the corpus, defaults to 50.
        workers : int, optional
            Number of threads to use for training the model, defaults to 1.
        epsilon : float, optional
            Constant used for clipping embeddings below a norm of one, defaults to 1e-5.
        burn_in : int, optional
            Number of epochs to use for burn-in initialization (0 means no burn-in), defaults to 0.
        burn_in_alpha : float, optional
            learning rate for burn-in initialization, defaults to 0.01, ignored if `burn_in` is 0.
        seed : int, optional
            seed for random to ensure reproducibility, defaults to 0.

        Returns
        --------
        PoincareModel instance

        """
        self.train_data = train_data
        self.wv = KeyedVectors()
        self.size = size
        self.train_alpha = alpha  # Learning rate for training
        self.burn_in_alpha = burn_in_alpha  # Learning rate for burn-in
        self.alpha = alpha  # Current learning rate
        self.negative = negative
        self.iter = iter
        self.workers = workers
        self.epsilon = epsilon
        self.burn_in = burn_in
        self.seed = seed
        self.random = random.Random(seed)
        self.np_random = np_random.RandomState(seed)
        self.init_range = (-0.001, 0.001)
        self.loss_grad = grad(PoincareModel.loss_fn)
        self.load_relations()
        self.init_embeddings()

    def load_relations(self):
        """Load relations from the train data and build vocab."""
        vocab = {}
        index2word = []
        all_relations = []  # List of all relation pairs
        term_relations = defaultdict(set)  # Mapping from node index to its related node indices

        for hypernym_pair in self.train_data:
            assert len(hypernym_pair) == 2, 'Relation pair has more than two items'
            for item in hypernym_pair:
                if item in vocab:
                    vocab[item].count += 1
                else:
                    vocab[item] = Vocab(count=1, index=len(index2word))
                    index2word.append(item)
            node_1, node_2 = hypernym_pair
            node_1_index, node_2_index = vocab[node_1].index, vocab[node_2].index
            term_relations[node_1_index].add(node_2_index)
            relation = (node_1_index, node_2_index)
            all_relations.append(relation)

        self.wv.vocab = vocab
        self.wv.index2word = index2word
        self.indices_set = set((range(len(index2word))))  # Set of all node indices
        self.indices_array = np.array(range(len(index2word)))  # Numpy array of all node indices
        counts = np.array([self.wv.vocab[index2word[i]].count for i in range(len(index2word))])
        self.node_probabilities = counts / counts.sum()
        self.node_probabilities_cumsum = np.cumsum(self.node_probabilities)
        self.all_relations = all_relations
        self.term_relations = term_relations
        self.negatives_buffer = []  # Buffer to store negative samples, to reduce calls to sampling method
        self.negatives_buffer_index = 0  # Position in buffer till which samples have been consumed
        self.negatives_buffer_size = 2000

    def init_embeddings(self):
        """Randomly initialize vectors for the items in the vocab."""
        shape = (len(self.wv.index2word), self.size)
        self.wv.syn0 = self.np_random.uniform(self.init_range[0], self.init_range[1], shape)

    def get_candidate_negatives(self):
        """
        Returns candidate negatives of size `self.negative` from the negative examples buffer.

        Returns
        --------
        numpy array
            numpy array of shape (`self.negative`,) containing indices of negative nodes.
        """

        if self.negatives_buffer_index >= len(self.negatives_buffer):
            # Note: np.random.choice much slower than random.sample for large populations, possible bottleneck
            uniform_numbers = np.random.random(self.negatives_buffer_size)
            cumsum_table_indices = np.searchsorted(self.node_probabilities_cumsum, uniform_numbers)
            self.negatives_buffer = self.indices_array[cumsum_table_indices]
            self.negatives_buffer_index = 0
        start_index = self.negatives_buffer_index
        end_index = start_index + self.negative
        candidate_negatives = self.negatives_buffer[start_index:end_index]
        self.negatives_buffer_index += self.negative
        return candidate_negatives

    @staticmethod
    def has_duplicates(array):
        seen = set()
        for value in array:
            if value in seen:
                return True
            seen.add(value)
        return False

    def sample_negatives(self, node_index):
        """
        Return a sample of negatives for the given node.

        Parameters
        ----------
        node_index : int
            index of the positive node for which negative samples are to be returned.

        Returns
        --------
        numpy array
            numpy array of shape (self.negative,) containing indices of negative nodes for the given node index.
        """
        node_relations = self.term_relations[node_index]
        positive_fraction = len(node_relations) / len(self.term_relations)
        if positive_fraction < 0.01:
            # If number of positive relations is a small fraction of total nodes
            # re-sample till no positively connected nodes are chosen
            indices = self.get_candidate_negatives()
            times_sampled = 1
            while len(set(indices) & node_relations) or self.has_duplicates(indices):
                times_sampled += 1
                indices = self.get_candidate_negatives()
            logger.debug('Sampled %d times, positive fraction %.5f', times_sampled, positive_fraction)
        else:
            # If number of positive relations is a significant fraction of total nodes
            # subtract positively connected nodes from set of choices and sample from the remaining
            valid_negatives = np.array(list(self.indices_set - node_relations))
            probs = self.node_probabilities[valid_negatives]
            probs /= probs.sum()
            indices = self.np_random.choice(valid_negatives, size=self.negative, p=probs)

        return list(indices)

    @staticmethod
    def loss_fn(matrix):
        """
        Given a numpy array with vectors for u, v and negative samples, computes loss value.

        Parameters
        ----------
        matrix : numpy array
            numpy array containing vectors for u, v and negative samples, of shape (2 + negative_size, dim).

        Returns
        -------
        float
            computed loss value.

        Notes
        -----
        Only used for autograd gradients, since autograd requires a specific function signature.
        """
        vector_u = matrix[0]
        vector_v = matrix[1]
        vectors_negative = matrix[2:]
        positive_distance = PoincareKeyedVectors.poincare_dist(vector_u, vector_v)
        negative_distances = np.array([
            PoincareKeyedVectors.poincare_dist(vector_u, vector_negative)
            for vector_negative in vectors_negative
        ])
        exp_negative_distances = np.exp(-negative_distances)
        exp_positive_distance = np.exp(-positive_distance)
        return -np.log(exp_positive_distance / (exp_positive_distance + exp_negative_distances.sum()))

    @staticmethod
    def clip_vectors(vectors, epsilon):
        """
        Clip vectors to have a norm of less than one.

        Parameters
        ----------
        vectors : numpy array
            can be 1-D,or 2-D (in which case the norm for each row is checked).
        epsilon : float
            parameter for numerical stability, each dimension of the vector is reduced by `epsilon`
            if the norm of the vector is greater than or equal to 1.
        Returns
        -------
        numpy array
            numpy array with norms clipped below 1.

        """
        one_d = len(vectors.shape) == 1
        threshold = 1 - epsilon
        if one_d:
            norm = np.linalg.norm(vectors)
            if norm < threshold:
                return vectors
            else:
                return vectors / norm - (np.sign(vectors) * epsilon)
        else:
            norms = np.linalg.norm(vectors, axis=1)
            if (norms < threshold).all():
                return vectors
            else:
                vectors[norms >= threshold] *= (threshold / norms[norms >= threshold])[:, np.newaxis]
                vectors[norms >= threshold] -= np.sign(vectors[norms >= threshold]) * epsilon
                return vectors

    def prepare_training_batch(self, relations, all_negatives, check_gradients=False):
        """
        Creates training batch and computes gradients and loss for the batch.

        Parameters
        ----------

        relations : list of tuples
            list of tuples of positive examples of the form (node_1_index, node_2_index).
        all_negatives : list of lists
            list of lists of negative samples for each node_1 in the positive examples.
        check_gradients : bool, optional
            whether to compare the computed gradients to autograd gradients for this batch, defaults to False.

        Returns
        -------
        PoincareBatch instance
            contains node indices, computed gradients and loss for the batch.
        """
        batch_size = len(relations)
        all_vectors = []
        indices_u, indices_v = [], []
        for relation, negatives in zip(relations, all_negatives):
            u, v = relation
            indices_u.append(u)
            indices_v.append(v)
            for negative in negatives:
                indices_v.append(negative)

        vectors_u = self.wv.syn0[indices_u]
        vectors_v = self.wv.syn0[indices_v].reshape((batch_size, 1 + self.negative, self.size))
        vectors_v = vectors_v.swapaxes(0,1).swapaxes(1,2)
        batch = PoincareBatch(vectors_u, vectors_v, indices_u, indices_v)
        batch.compute_all()

        if check_gradients:
            max_diff = 0.0
            for i, (relation, negatives) in enumerate(zip(relations, all_negatives)):
                u, v = relation
                auto_gradients = self.loss_grad(np.vstack((self.wv.syn0[u], self.wv.syn0[[v] + negatives])))
                computed_gradients = np.vstack((batch.gradients_u[:, i], batch.gradients_v[:, :, i]))
                diff = np.abs(auto_gradients - computed_gradients).max()
                if diff > max_diff:
                    max_diff = diff
            logger.info('Max difference between gradients: %.10f', max_diff)
            if max_diff > 1e-8:
                logger.warning(
                    'Max difference between computed gradients and autograd gradients %.10f, '
                    'greater than tolerance %.10f', max_diff, 1e-8)
        return batch

    def sample_negatives_batch(self, nodes):
        """
        Return negative examples for each node in the given nodes.

        Parameters
        ----------
        nodes : list
            list of node indices for which negative samples are to be returned.

        Returns
        -------
        list of lists
            each inner list is a list of negative sample for a single node in the input list.
        """
        all_indices = [self.sample_negatives(node) for node in nodes]
        return all_indices

    def train_on_batch(self, relations, check_gradients=False):
        """
        Performs training for a single training batch.

        Parameters
        ----------
        relations : list of tuples
            list of tuples of positive examples of the form (node_1_index, node_2_index).
        check_gradients : bool, optional
            whether to compare the computed gradients to autograd gradients for this batch, defaults to False.

        Returns
        -------
        PoincareBatch instance
            the batch that was just trained on, contains computed loss for the batch.
        """
        all_negatives = self.sample_negatives_batch([relation[0] for relation in relations])
        batch = self.prepare_training_batch(relations, all_negatives, check_gradients)
        self.update_vectors_batch(batch)
        return batch

    @staticmethod
    def handle_duplicates(vector_updates, node_indices):
        """
        Handles occurrences of multiple updates to the same node in a batch of vector updates.

        Parameters
        ----------
        vector_updates : numpy array
            array with each row containing updates to be performed on a certain node.
        node_indices : list
            node indices on which the above updates are to be performed on.

        Notes
        -----
        Mutates the `vector_updates` array.

        Required because array[[2, 1, 2]] += np.array([-0.5, 1.0, 0.5]) performs only the last update
        on the row at index 2.
        """
        counts = Counter(node_indices)
        for node_index, count in counts.items():
            if count == 1:
                continue
            positions = [i for i, index in enumerate(node_indices) if index == node_index]
            # Move all updates to the same node to the last such update, zeroing all the others
            vector_updates[positions[-1]] = vector_updates[positions].sum(axis=0)
            vector_updates[positions[:-1]] = 0

    def update_vectors_batch(self, batch):
        """
        Updates vectors for nodes in the given batch.

        Parameters
        ----------
        batch : PoincareBatch instance
            batch containing computed gradients and node indices of the batch for which updates are to be done.

        Notes
        -----
        Mutates the `syn0` array.
        """
        grad_u, grad_v = batch.gradients_u, batch.gradients_v
        indices_u, indices_v = batch.indices_u, batch.indices_v
        batch_size = len(indices_u)

        u_updates = (self.alpha * (batch.alpha ** 2) / 4 * grad_u).T
        self.handle_duplicates(u_updates, indices_u)

        self.wv.syn0[indices_u] -= u_updates
        self.wv.syn0[indices_u] = self.clip_vectors(self.wv.syn0[indices_u], self.epsilon)

        v_updates = self.alpha * (batch.beta ** 2)[:, np.newaxis] / 4 * grad_v
        v_updates = v_updates.swapaxes(1, 2).swapaxes(0, 1)
        v_updates = v_updates.reshape(((1 + self.negative) * batch_size, self.size))
        self.handle_duplicates(v_updates, indices_v)

        self.wv.syn0[indices_v] -= v_updates
        self.wv.syn0[indices_v] = self.clip_vectors(self.wv.syn0[indices_v], self.epsilon)

    def train(self, batch_size=10, print_every=1000):
        """Trains Poincare embeddings using loaded data and model parameters."""
        if self.burn_in > 0:
            self.alpha = self.burn_in_alpha
            self.train_batchwise(epochs=self.burn_in, batch_size=batch_size, print_every=print_every)
        self.alpha = self.train_alpha
        self.train_batchwise(batch_size=batch_size, print_every=print_every)

    def train_batchwise(self, epochs=None, batch_size=10, print_every=1000):
        """
        Trains Poincare embeddings using specified parameters.

        Parameters
        ----------
        epochs : int or None, optional
            Number of epochs after which training ends, if `None`, runs for `self.iter` epochs, default `None`.
        batch_size : int, optional
            Number of examples to train on in a single batch, defaults to 10.
        print_every : int or None, optional
            Prints progress and average loss after every `print_every` batches, defaults to 1000.
        """
        if self.workers > 1:
            raise NotImplementedError("Multi-threaded version not implemented yet")
        if epochs is None:
            epochs = self.iter
        for epoch in range(1, epochs + 1):
            indices = list(range(len(self.all_relations)))
            self.np_random.shuffle(indices)
            avg_loss = 0.0
            last_time = time.time()
            for batch_num, i in enumerate(range(0, len(indices), batch_size), start=1):
                should_print = not (batch_num % print_every)
                batch_indices = indices[i:i+batch_size]
                relations = [self.all_relations[idx] for idx in batch_indices]
                result = self.train_on_batch(relations, check_gradients=should_print)
                avg_loss += result.loss
                if should_print:
                    avg_loss /= print_every
                    time_taken = time.time() - last_time
                    speed = print_every * batch_size / time_taken
                    logger.info(
                        'Training on epoch %d, examples #%d-#%d, loss: %.2f'
                        % (epoch, i, i + batch_size, avg_loss))
                    logger.info(
                        'Time taken for %d examples: %.2f s, %.2f examples / s'
                        % (print_every * batch_size, time_taken, speed))
                    last_time = time.time()
                    avg_loss = 0.0


class PoincareBatch(object):
    """
    Class for computing Poincare distances, gradients and loss for a training batch,
    and storing intermediate state to avoid recomputing multiple times.
    """
    def __init__(self, vectors_u, vectors_v, indices_u, indices_v):
        """
        Initialize instance with sets of vectors for which distances are to be computed.

        Parameters
        ----------
        vectors_u : numpy array
            expected shape (batch_size, dim).
            vectors of all nodes `u` in the batch.
        vectors_v : numpy array
            expected shape (1 + neg_size, dim, batch_size).
            vectors of all hypernym nodes `v` and negatively sampled nodes `v'`,
            for each node `u` in the batch.
        indices_u : list
            list of node indices for each of the vectors in `vectors_u`.
        indices_v : list
            nested list of lists, each of which is a  list of node indices
            for each of the vectors in `vectors_v` for a specific node `u`.

        Returns
        -------
        PoincareBatch instance
        """
        self.vectors_u = vectors_u.T[np.newaxis, :, :]  # (1, dim, batch_size)
        self.vectors_v = vectors_v  # (1 + neg_size, dim, batch_size)
        self.indices_u = indices_u
        self.indices_v = indices_v

        self.poincare_dists = None
        self.euclidean_dists = None

        self.norms_u = None
        self.norms_v = None
        self.alpha = None
        self.beta = None
        self.gamma = None

        self.gradients_u = None
        self.distance_gradients_u = None
        self.gradients_v = None
        self.distance_gradients_v = None

        self.loss = None

        self.distances_computed = False
        self.gradients_computed = False
        self.distance_gradients_computed = False
        self.loss_computed = False

    def compute_all(self):
        """Convenience method to perform all computations."""
        self.compute_distances()
        self.compute_distance_gradients()
        self.compute_gradients()
        self.compute_loss()

    def compute_distances(self):
        """Compute and store norms, euclidean distances and poincare distances between input vectors."""
        if self.distances_computed:
            return
        euclidean_dists = np.linalg.norm(self.vectors_u - self.vectors_v, axis=1)  # (1 + neg_size, batch_size)
        norms_u = np.linalg.norm(self.vectors_u, axis=1)  # (1, batch_size)
        norms_v = np.linalg.norm(self.vectors_v, axis=1)  # (1 + neg_size, batch_size)
        alpha = 1 - norms_u ** 2  # (1, batch_size)
        beta = 1 - norms_v ** 2  # (1 + neg_size, batch_size)
        gamma = 1 + 2 * (
                (euclidean_dists ** 2) / (alpha * beta)
            )  # (1 + neg_size, batch_size)
        poincare_dists = np.arccosh(gamma)  # (1 + neg_size, batch_size)
        exp_negative_distances = np.exp(-poincare_dists)  # (1 + neg_size, batch_size)
        Z = exp_negative_distances.sum(axis=0)  # (batch_size)

        self.euclidean_dists = euclidean_dists
        self.poincare_dists = poincare_dists
        self.exp_negative_distances = exp_negative_distances
        self.Z = Z
        self.gamma = gamma
        self.norms_u = norms_u
        self.norms_v = norms_v
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma

        self.distances_computed = True

    def compute_gradients(self):
        """Compute and store gradients of loss function for all input vectors."""
        if self.gradients_computed:
            return
        self.compute_distances()
        self.compute_distance_gradients()

        gradients_v = -self.exp_negative_distances[:, np.newaxis, :] * self.distance_gradients_v  # (1 + neg_size, dim, batch_size)
        gradients_v /= self.Z  # (1 + neg_size, dim, batch_size)
        gradients_v[0] += self.distance_gradients_v[0]

        gradients_u = -self.exp_negative_distances[:, np.newaxis, :] * self.distance_gradients_u  # (1 + neg_size, dim, batch_size)
        gradients_u /= self.Z  # (1 + neg_size, dim, batch_size)
        gradients_u = gradients_u.sum(axis=0)  # (dim, batch_size)
        gradients_u += self.distance_gradients_u[0]

        assert(not np.isnan(gradients_u).any())
        assert(not np.isnan(gradients_v).any())
        self.gradients_u = gradients_u
        self.gradients_v = gradients_v

        self.gradients_computed = True

    def compute_distance_gradients(self):
        """Compute and store partial derivatives of poincare distance d(u, v) w.r.t all u and all v."""
        if self.distance_gradients_computed:
            return
        self.compute_distances()

        euclidean_dists_squared = self.euclidean_dists ** 2  # (1 + neg_size, batch_size)
        c_ = (4 / (self.alpha * self.beta * np.sqrt(self.gamma ** 2 - 1)))[:, np.newaxis, :]  # (1 + neg_size, 1, batch_size)
        u_coeffs = ((euclidean_dists_squared + self.alpha) / self.alpha)[:, np.newaxis, :]  # (1 + neg_size, 1, batch_size)
        distance_gradients_u = u_coeffs * self.vectors_u - self.vectors_v  # (1 + neg_size, dim, batch_size)
        distance_gradients_u *= c_  # (1 + neg_size, dim, batch_size)

        nan_gradients = self.gamma == 1  # (1 + neg_size, batch_size)
        if nan_gradients.any():
            distance_gradients_u.swapaxes(1, 2)[nan_gradients] = 0
        self.distance_gradients_u = distance_gradients_u

        v_coeffs = ((euclidean_dists_squared + self.beta) / self.beta)[:, np.newaxis, :]  # (1 + neg_size, 1, batch_size)
        distance_gradients_v = v_coeffs * self.vectors_v - self.vectors_u  # (1 + neg_size, dim, batch_size)
        distance_gradients_v *= c_  # (1 + neg_size, dim, batch_size)

        if nan_gradients.any():
            distance_gradients_v.swapaxes(1, 2)[nan_gradients] = 0
        self.distance_gradients_v = distance_gradients_v

        self.distance_gradients_computed = True

    def compute_loss(self):
        """Compute and store loss value for the given batch of examples."""
        if self.loss_computed:
            return
        self.compute_distances()

        self.loss = -np.log(self.exp_negative_distances[0] / self.Z).sum()  # scalar
        self.loss_computed = True


class PoincareKeyedVectors(KeyedVectors):
    """
    Class to contain vectors and vocab for the PoincareModel training class,
    can be used to perform operations on the vectors such as vector lookup, distance etc.

    """
    @staticmethod
    def poincare_dist(vector_1, vector_2):
        """Return poincare distance between two vectors."""
        norm_1 = np.linalg.norm(vector_1)
        norm_2 = np.linalg.norm(vector_2)
        euclidean_dist = np.linalg.norm(vector_1 - vector_2)
        if euclidean_dist == 0.0:
            return 0.0
        else:
            return np.arccosh(
                1 + 2 * (
                    (euclidean_dist ** 2) / ((1 - norm_1 ** 2) * (1 - norm_2 ** 2))
                )
            )
    # TODO: Add other KeyedVector supported methods - most_similar, etc.


class PoincareData(object):
    """
    Class to stream hypernym relations for `PoincareModel` from a tsv-like file

    """
    def __init__(self, file_path, encoding='utf8', delimiter='\t'):
        """
        Initialize instance from file containing one hypernym pair per line.

        Parameters
        ----------
        file_path : str
            path to file containing one hypernym pair per line, separated by `delimiter`.
        encoding : str, optional
            character encoding of the input file.
        delimiter : str, optional
            delimiter character for each hypernym pair.

        Returns
        -------
        PoincareData instance
        """

        self.file_path = file_path
        self.encoding = encoding
        self.delimiter = delimiter

    def stream_lines(self):
        """
        Streams lines from self.file_path decoded into unicode strings.

        Yields
        -------
        str (unicode)
            Single line from input file.
        """
        with smart_open(self.file_path, 'rb') as f:
            for line in f:
                yield line.decode(self.encoding)

    def __iter__(self):
        """
        Streams relations from self.file_path decoded into unicode strings.

        Yields
        -------
        2-tuple (unicode)
            Hypernym relation from input file.
        """
        reader = csv.reader(self.stream_lines(), delimiter=self.delimiter)
        for row in reader:
            yield tuple(row)

