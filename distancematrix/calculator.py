import numpy as np
import time
import random
from collections import OrderedDict
from math import ceil
from distancematrix.interrupt_util import interrupt_catcher
from distancematrix.util import diag_length


class Calculator(object):
    """
    Class that organises the calculation and processing of distance matrix values between generators and
    consumers.

    In order to do useful work, generators and consumers need to be added. Generators will use the input
    query and series to form a distance matrix. Consumers process these values in a way that is useful.
    """

    def __init__(self, m, series, query=None, trivial_match_buffer=None):
        """
        Initialises a new calculator (without any generators/consumers).

        :param m: subsequence length
        :param series: 1D or 2D (dimensions x datapoints) array
        :param query: 1D or 2D (dimensions x datapoints) array, if None, a self-join on series is performed
        :param trivial_match_buffer: used only in case of a self-join, the number of values next to the main diagonal
        (of the distance matrix) to skip. If None, defaults to m/2. Any consumers will either not receive values
        (in case of diagonal calculation) or Infinity values (in case of column calculation).
        """
        self._self_join = query is None

        self.series = np.atleast_2d(series).astype(np.float, copy=True)
        if not self._self_join:
            self.query = np.atleast_2d(query).astype(np.float, copy=True)
        else:
            self.query = self.series

        if self.series.ndim != 2:
            raise RuntimeError("Series should be 1D or 2D ndarray.")
        if self.query.ndim != 2:
            raise RuntimeError("Query should be 1D or 2D ndarray.")

        self.n_dim = self.series.shape[0]
        if self.n_dim != self.query.shape[0]:
            raise RuntimeError("Dimensions of series and query do not match.")

        self.m = m
        self.num_series_subseq = self.series.shape[1] - m + 1
        self.num_query_subseq = self.query.shape[1] - m + 1

        if self._self_join:
            if trivial_match_buffer is None:
                trivial_match_buffer = m // 2
            if trivial_match_buffer not in range(-1, self.num_series_subseq):
                raise RuntimeError("Invalid value for trivial_match_buffer: " + str(trivial_match_buffer))
            self.trivial_match_buffer = trivial_match_buffer
        else:
            self.trivial_match_buffer = -1

        # Generators calculate distance values from the series and query
        self._generators = OrderedDict()
        # Consumers process the calculated distance values
        self._consumers = OrderedDict()

        # Tracking column calculations
        self._last_column_calculated = -1

        # Tracking diagonal calculations
        if not self._self_join:
            self._diagonal_calc_order = np.arange(-self.num_query_subseq + 1, self.num_series_subseq)
            self._diagonal_values_total = self.num_query_subseq * self.num_series_subseq
        else:
            self._diagonal_calc_order = np.arange(self.trivial_match_buffer + 1, self.num_series_subseq)
            # Upper half of a square with size a = a * (a+1) / 2
            temp = self.num_series_subseq - self.trivial_match_buffer - 1
            self._diagonal_values_total = temp * (temp + 1) // 2
        random.shuffle(self._diagonal_calc_order, random.Random(0).random)
        self._diagonal_calc_list_next_index = 0
        self._diagonal_values_calculated = 0
        self._diagonal_calc_time = 0

    @property
    def num_dist_matrix_values(self):
        return self.num_query_subseq * self.num_series_subseq

    @property
    def generators(self):
        return list(self._generators.keys())

    @property
    def consumers(self):
        return list(self._consumers.keys())

    def add_generator(self, input_dim, generator):
        if input_dim < 0 or input_dim >= self.n_dim:
            raise ValueError("Invalid input_dim, should be in range [0, %s]" % self.n_dim)

        if not self._self_join:
            bound_gen = generator.prepare(self.m, self.series[input_dim, :], self.query[input_dim, :])
        else:
            bound_gen = generator.prepare(self.m, self.series[input_dim, :])

        self._generators[bound_gen] = input_dim

    def add_consumer(self, generator_ids, consumer):
        gen_dims = len(generator_ids)
        q = self.query.shape[1]
        n = self.series.shape[1]
        consumer.initialise(gen_dims, q-self.m+1, n-self.m+1)
        self._consumers[consumer] = generator_ids

    def calculate_columns(self, start=None, upto=1., print_progress=False):
        """
        Calculates columns of the distance matrix. The calculator keeps track of the rightmost column that was
        already calculated and will use it as starting position unless the start position is provided.

        Note that the generators are optimised for calculating consecutive columns from left to right.

        :param start: int for absolute position, float for relative position. The first column to calculate.
        If None, continues from the rightmost column that was calculated so far.
        :param upto: int for absolute position, float for relative position. The last column (exclusive) to calculate.
        :param print_progress: print progress to console?
        :return: None
        """
        if start is None:
            start = self._last_column_calculated + 1
        start = _ratio_to_int(start, self.num_series_subseq)
        current_column = start
        upto = _ratio_to_int(upto, self.num_series_subseq)

        generators = list(self._generators.keys())
        generators_needed_ids = list(set(id for id_list in self._consumers.values() for id in id_list))
        column_dists = np.full((len(self._generators), self.num_query_subseq), np.nan, dtype=np.float)

        start_time = time.time()

        with interrupt_catcher() as is_interrupted:
            while current_column < upto and not is_interrupted():
                for generator_id in generators_needed_ids:  # todo: parallel
                    generator = generators[generator_id]
                    column_dists[generator_id, :] = generator.calc_column(current_column)

                if self.trivial_match_buffer >= 0:
                    trivial_match_start = max(0, current_column - self.trivial_match_buffer)
                    trivial_match_end = current_column + self.trivial_match_buffer + 1
                    column_dists[:, trivial_match_start : trivial_match_end] = np.inf

                for consumer, generator_ids in self._consumers.items():  # todo: parallel
                    consumer.process_column(current_column, column_dists[generator_ids, :])

                self._last_column_calculated = max(current_column, self._last_column_calculated)
                current_column += 1

                if print_progress:
                    columns_calculated = current_column - start
                    columns_remaining = upto + 1 - current_column
                    print("\r{0:5.3f}% {1:10.1f} sec".format(
                        columns_calculated / (upto + 1 - start) * 100,
                        (time.time() - start_time) / columns_calculated * columns_remaining
                    ), end="")

    def calculate_diagonals(self, partial=1., print_progress=False):
        """
        Calculates diagonals of the distance matrix. The advantage of calculating diagonals is that values are spread
        over the entire distance matrix, which can provide a quick approximation for any consumer.

        :param partial: int for a number of values, float for relative number of values. The number of distance
        matrix values that should be calculated (including the counts of previous diagonals calulated).
        :param print_progress: print progress to the console
        :return: None
        """
        generators = list(self._generators.keys())
        generators_needed_ids = list(set(id for id_list in self._consumers.values() for id in id_list))
        max_diagonal_length = min(self.num_query_subseq, self.num_series_subseq)
        diag_dists = np.full((len(self._generators), max_diagonal_length), np.nan, dtype=np.float)

        values_needed = _ratio_to_int(partial, self._diagonal_values_total)

        with interrupt_catcher() as is_interrupted:
            while self._diagonal_values_calculated < values_needed and not is_interrupted():
                start_time = time.time()

                # Diagonal: 0 is the main diagonal, 1 is one above the main diagonal, etc...
                diagonal = self._diagonal_calc_order[self._diagonal_calc_list_next_index]
                diagonal_length = diag_length(self.num_query_subseq, self.num_series_subseq, diagonal)
                diagonal_values = diag_dists[:, :diagonal_length]

                for generator_id in generators_needed_ids:  # todo: parallel
                    generator = generators[generator_id]
                    diagonal_values[generator_id, :] = generator.calc_diagonal(diagonal)

                for consumer, generator_ids in self._consumers.items():  # todo: parallel
                    values_to_consume = diagonal_values[generator_ids, :]
                    consumer.process_diagonal(diagonal, values_to_consume)
                    if self._self_join:
                        consumer.process_diagonal(-diagonal, values_to_consume)

                self._diagonal_values_calculated += diagonal_length
                self._diagonal_calc_list_next_index += 1

                self._diagonal_calc_time += time.time() - start_time
                if print_progress:
                    local_progress = self._diagonal_values_calculated / values_needed
                    global_progress = self._diagonal_values_calculated / self._diagonal_values_total
                    avg_time_per_value = self._diagonal_calc_time / self._diagonal_values_calculated
                    time_left = avg_time_per_value * (values_needed - self._diagonal_values_calculated)
                    print("\r{0:5.3f}% {1:10.1f} sec ({2:5.3f}% total)".
                          format(local_progress * 100, time_left, global_progress * 100), end="")


def _ratio_to_int(ratio_or_result, maximum):
    if type(ratio_or_result) == float:
        if ratio_or_result < 0 or ratio_or_result > 1:
            raise ValueError("Value should be in range [0, 1].")

        return ceil(ratio_or_result * maximum)

    if type(ratio_or_result) == int:
        return ratio_or_result

    raise RuntimeError("Invalid type, should be int or float.")


def _find_invalid_subseq_idxs(invalid_data_idxs, dims, m, min_result_idx, max_result_idx):
    """
    Given indices of invalid data points, calculate the list of indices of affected subsequences.

    :param invalid_data_idxs: 2-element tuple of indices of invalid data points (ourput of np.nonzero)
    :param dims: number of data dimensions
    :param m: subsequence length
    :param min_result_idx: minimum allowed index in the result
    :param max_result_idx: maximum allowed index in the result
    :return: a list containg dims lists, each containing indices of subsequences affected by invalid data points
    """
    result_set = [set() for i in range(dims)]

    for dim, invalid_idx in np.transpose(invalid_data_idxs):
        affected_subsequences = range(max(min_result_idx, invalid_idx - m + 1, min(invalid_idx + m, max_result_idx)))
        result_set[dim].update(affected_subsequences)

    return [np.array(list(s), dtype=np.int64) for s in result_set]
