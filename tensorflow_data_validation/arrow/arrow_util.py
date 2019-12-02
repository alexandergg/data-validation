# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License
"""Util functions regarding to Arrow objects."""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import numpy as np
from tensorflow_data_validation import types
from tensorflow_data_validation.pyarrow_tf import pyarrow as pa
from tfx_bsl.arrow import array_util
from typing import Iterable, Optional, Text, Tuple


def get_weight_feature(input_table: pa.Table,
                       weight_column: Text) -> np.ndarray:
  """Gets the weight column from the input table.

  Args:
    input_table: Input table.
    weight_column: Name of the column containing the weight.

  Returns:
    A numpy array containing the weights of the examples in the input table.

  Raises:
    ValueError: If the weight feature is not present in the input table or is
        not a valid weight feature (must be of numeric type and have a
        single value for each example).
  """
  try:
    weights = input_table.column(weight_column).data.chunk(0)
  except KeyError:
    raise ValueError('Weight feature "{}" not present in the input '
                     'table.'.format(weight_column))

  # Before flattening, check that there is a single value for each example.
  weight_lengths = array_util.ListLengthsFromListArray(weights).to_numpy()
  if not np.all(weight_lengths == 1):
    raise ValueError(
        'Weight feature "{}" must have exactly one value in each example.'
        .format(weight_column))
  weights = weights.flatten()
  # Before converting to numpy view, check the type (cannot convert string and
  # binary arrays to numpy view).
  weights_type = weights.type
  if pa.types.is_string(weights_type) or pa.types.is_binary(weights_type):
    raise ValueError(
        'Weight feature "{}" must be of numeric type. Found {}.'.format(
            weight_column, weights_type))
  return np.asarray(weights)


def get_array(
    table: pa.Table, query_path: types.FeaturePath,
    weight_column: Optional[Text]) -> Tuple[pa.Array, Optional[np.ndarray]]:
  """Retrieve a nested array (and optionally weights) from a table.

  It assumes all the columns in `table` have only one chunk.
  It assumes `table` contains only arrays of the following supported types:
    - list<primitive>
    - list<struct<[Ts]>> where Ts are the types of the fields in the struct
      type, and they can only be one of the supported types
      (recursion intended).

  If the provided path refers to a leaf in the table, then a ListArray with a
  primitive element type will be returned. If the provided path does not refer
  to a leaf, a ListArray with a StructArray element type will be returned.

  Args:
    table: The Table whose arrays to be visited. It is assumed that the table
      contains only one chunk.
    query_path: The FeaturePath to lookup in the table.
    weight_column: The name of the weight column, or None. The elements of
      the weight column should be lists of numerics, and each list should
      contain only one value.

  Returns:
    A tuple. The first term is the feature array and the second term is the
    weight array for the feature array (i.e. weights[i] is the weight for
    array[i]).

  Raises:
    ValueError: When the weight column is not a list array whose elements are
      1-element lists.
    KeyError: When the query_path is empty, or cannot be found in the table and
      its nested struct arrays.
  """

  def _recursion_helper(
      query_path: types.FeaturePath, array: pa.Array,
      weights: Optional[np.ndarray]
  ) -> Tuple[pa.Array, Optional[np.ndarray]]:
    """Recursion helper."""
    array_type = array.type
    if (not query_path or not pa.types.is_list(array_type) or
        not pa.types.is_struct(array_type.value_type)):
      return array, weights
    flat_struct_array = array.flatten()
    flat_weights = None
    if weights is not None:
      flat_weights = weights[
          array_util.GetFlattenedArrayParentIndices(array).to_numpy()]

    step = query_path.steps()[0]
    try:
      child_array = flat_struct_array.field(step)
    except KeyError:
      raise KeyError('query_path step, {}, not in struct.'.format(step))
    relative_path = types.FeaturePath(query_path.steps()[1:])
    return _recursion_helper(relative_path, child_array, flat_weights)

  if not query_path:
    raise KeyError('query_path must be non-empty.')
  column_name = query_path.steps()[0]
  try:
    array = table.column(column_name).data.chunk(0)
  except KeyError:
    raise KeyError('query_path step 0, {}, not in table.'.format(column_name))
  array_path = types.FeaturePath(query_path.steps()[1:])

  weights = None
  if weight_column is not None:
    weights = get_weight_feature(table, weight_column)
  return _recursion_helper(array_path, array, weights)


def enumerate_arrays(
    table: pa.Table, weight_column: Optional[Text], enumerate_leaves_only: bool
) -> Iterable[Tuple[types.FeaturePath, pa.Array, Optional[np.ndarray]]]:
  """Enumerates arrays in a Table.

  It assumes all the columns in `table` have only one chunk.
  It assumes `table` contains only arrays of the following supported types:
    - list<primitive>
    - list<struct<[Ts]>> where Ts are the types of the fields in the struct
      type, and they can only be one of the supported types
      (recursion intended).

  It enumerates each column (i.e. array, because there is only one chunk) in
  the table (also see `enumerate_leaves_only`) If an array is of type
  list<struct<[Ts]>>, then it flattens the outermost list, then enumerates the
  array of each field in the result struct<[Ts]> array, and continues
  recursively. The weights get "aligned" automatically in this process,
  therefore weights, the third term in the returned tuple always has array[i]'s
  weight being weights[i].

  Args:
    table: The Table whose arrays to be visited. It is assumed that the table
      contains only one chunk.
    weight_column: The name of the weight column, or None. The elements of
      the weight column should be lists of numerics, and each list should
      contain only one value.
    enumerate_leaves_only: If True, only enumerate "leaf" arrays.
      Otherwise, also enumerate the struct arrays where the leaf arrays are
      contained.

  Yields:
    A tuple. The first term is the path of the feature; the second term is
    the feature array and the third term is the weight array for the feature
    array (i.e. weights[i] is the weight for array[i]).

  Raises:
    ValueError: When the weight column is not a list array whose elements are
      1-element lists.
  """

  def _recursion_helper(
      feature_path: types.FeaturePath, array: pa.Array,
      weights: Optional[np.ndarray]
  ) -> Iterable[Tuple[types.FeaturePath, pa.Array, Optional[np.ndarray]]]:
    """Recursion helper."""
    array_type = array.type
    if (pa.types.is_list(array_type) and
        pa.types.is_struct(array_type.value_type)):
      if not enumerate_leaves_only:
        yield (feature_path, array, weights)
      flat_struct_array = array.flatten()
      flat_weights = None
      if weights is not None:
        flat_weights = weights[
            array_util.GetFlattenedArrayParentIndices(array).to_numpy()]
      for field in flat_struct_array.type:
        field_name = field.name
        # use "yield from" after PY 3.3.
        for e in _recursion_helper(
            feature_path.child(field_name),
            flat_struct_array.field(field_name), flat_weights):
          yield e
    else:
      yield (feature_path, array, weights)

  weights = None
  if weight_column is not None:
    weights = get_weight_feature(table, weight_column)
  for column_name, column in zip(table.schema.names, table.itercolumns()):
    # use "yield from" after PY 3.3.
    for e in _recursion_helper(
        types.FeaturePath([column_name]), column.data.chunk(0), weights):
      yield e
