# Copyright 2021 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.orchestration.experimental.core.pipeline_state."""

import dataclasses
import json
import os
import time
from typing import List
from unittest import mock

from absl.testing import parameterized
import tensorflow as tf
from tfx.dsl.io import fileio
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import env
from tfx.orchestration.experimental.core import event_observer
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.experimental.core import task_gen_utils
from tfx.orchestration.experimental.core import test_utils
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import metadata_pb2
from tfx.proto.orchestration import pipeline_pb2
from tfx.proto.orchestration import run_state_pb2
from tfx.utils import json_utils
from tfx.utils import status as status_lib
import ml_metadata as mlmd
from ml_metadata.proto import metadata_store_pb2


def _test_pipeline(
    pipeline_id,
    execution_mode: pipeline_pb2.Pipeline.ExecutionMode = (
        pipeline_pb2.Pipeline.ASYNC
    ),
    param=1,
    pipeline_nodes: List[str] = None,
    pipeline_run_id: str = 'run0',
    pipeline_root: str = '',
):
  pipeline = pipeline_pb2.Pipeline()
  pipeline.pipeline_info.id = pipeline_id
  pipeline.execution_mode = execution_mode
  if pipeline_nodes:
    for node in pipeline_nodes:
      pipeline.nodes.add().pipeline_node.node_info.id = node
    pipeline.nodes[0].pipeline_node.parameters.parameters[
        'param'
    ].field_value.int_value = param
  if execution_mode == pipeline_pb2.Pipeline.SYNC:
    pipeline.runtime_spec.pipeline_run_id.field_value.string_value = (
        pipeline_run_id
    )
  pipeline.runtime_spec.pipeline_root.field_value.string_value = pipeline_root
  return pipeline


def _add_sub_pipeline(
    pipeline: pipeline_pb2.Pipeline,
    sub_pipeline_id,
    sub_pipeline_nodes: List[str],
    sub_pipeline_run_id: str,
):
  sub_pipeline = pipeline_pb2.Pipeline()
  sub_pipeline.pipeline_info.id = sub_pipeline_id
  sub_pipeline.execution_mode = pipeline_pb2.Pipeline.SYNC

  for node_id in sub_pipeline_nodes:
    pipeline_or_node = sub_pipeline.nodes.add()
    pipeline_or_node.pipeline_node.node_info.id = node_id
    # Top layer pipeline run context
    context1 = pipeline_or_node.pipeline_node.contexts.contexts.add()
    context1.type.name = 'pipeline_run'
    context1.name.field_value.string_value = 'run0'
    # Current layer pipeline run context
    context2 = pipeline_or_node.pipeline_node.contexts.contexts.add()
    context2.type.name = 'pipeline_run'
    context2.name.field_value.string_value = sub_pipeline_run_id
  sub_pipeline.runtime_spec.pipeline_run_id.field_value.string_value = (
      sub_pipeline_run_id
  )
  pipeline.nodes.add().sub_pipeline.CopyFrom(sub_pipeline)


class NodeStateTest(test_utils.TfxTest):

  def test_node_state_update(self):
    node_state = pstate.NodeState()
    self.assertEqual(pstate.NodeState.STARTED, node_state.state)
    self.assertIsNone(node_state.status)

    status = status_lib.Status(code=status_lib.Code.CANCELLED, message='foobar')
    node_state.update(pstate.NodeState.STOPPING, status)
    self.assertEqual(pstate.NodeState.STOPPING, node_state.state)
    self.assertEqual(status, node_state.status)

  @mock.patch.object(pstate, 'time')
  def test_node_state_history(self, mock_time):
    mock_time.time.return_value = time.time()
    node_state = pstate.NodeState()
    self.assertEqual([], node_state.state_history)

    status = status_lib.Status(code=status_lib.Code.CANCELLED, message='foobar')
    node_state.update(pstate.NodeState.STOPPING, status)
    self.assertEqual(
        [
            pstate.StateRecord(
                state=pstate.NodeState.STARTED,
                backfill_token='',
                status_code=None,
                update_time=mock_time.time.return_value,
            )
        ],
        node_state.state_history,
    )

    node_state.update(pstate.NodeState.STOPPED)
    self.assertEqual(
        [
            pstate.StateRecord(
                state=pstate.NodeState.STARTED,
                backfill_token='',
                status_code=None,
                update_time=mock_time.time.return_value,
            ),
            pstate.StateRecord(
                state=pstate.NodeState.STOPPING,
                backfill_token='',
                status_code=status_lib.Code.CANCELLED,
                update_time=mock_time.time.return_value,
            ),
        ],
        node_state.state_history,
    )

  def test_node_state_json(self):
    node_state = pstate.NodeState.from_json_dict(
        {'state': pstate.NodeState.STARTED}
    )
    self.assertTrue(hasattr(node_state, 'state'))
    self.assertTrue(hasattr(node_state, 'last_updated_time'))


class TestEnv(env._DefaultEnv):

  def __init__(self, base_dir, max_str_len):
    self.base_dir = base_dir
    self.max_str_len = max_str_len

  def get_base_dir(self):
    return self.base_dir

  def max_mlmd_str_value_length(self):
    return self.max_str_len


class PipelineIRCodecTest(test_utils.TfxTest):

  def setUp(self):
    super().setUp()
    self._pipeline_root = os.path.join(
        os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR', self.get_temp_dir()),
        self.id(),
    )

  def test_encode_decode_no_base_dir(self):
    with TestEnv(None, None):
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pipeline_encoded = pstate._PipelineIRCodec.get().encode(pipeline)
    self.assertEqual(
        pipeline,
        pstate._base64_decode_pipeline(pipeline_encoded),
        'Expected pipeline IR to be base64 encoded.',
    )
    self.assertEqual(
        pipeline, pstate._PipelineIRCodec.get().decode(pipeline_encoded)
    )

  def test_encode_decode_with_base_dir(self):
    with TestEnv(self._pipeline_root, None):
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pipeline_encoded = pstate._PipelineIRCodec.get().encode(pipeline)
    self.assertEqual(
        pipeline,
        pstate._base64_decode_pipeline(pipeline_encoded),
        'Expected pipeline IR to be base64 encoded.',
    )
    self.assertEqual(
        pipeline, pstate._PipelineIRCodec.get().decode(pipeline_encoded)
    )

  def test_encode_decode_exceeds_max_len(self):
    with TestEnv(self._pipeline_root, 0):
      pipeline = _test_pipeline(
          'pipeline1',
          pipeline_nodes=['Trainer'],
          pipeline_root=self.create_tempdir().full_path,
      )
      pipeline_encoded = pstate._PipelineIRCodec.get().encode(pipeline)
    self.assertEqual(
        pipeline, pstate._PipelineIRCodec.get().decode(pipeline_encoded)
    )
    self.assertEqual(
        pstate._PipelineIRCodec._PIPELINE_IR_URL_KEY,
        next(iter(json.loads(pipeline_encoded).keys())),
        'Expected pipeline IR URL to be stored as json.',
    )


class PipelineStateTest(test_utils.TfxTest, parameterized.TestCase):

  def setUp(self):
    super().setUp()
    pipeline_root = os.path.join(
        os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR', self.get_temp_dir()),
        self.id(),
    )

    # Makes sure multiple connections within a test always connect to the same
    # MLMD instance.
    metadata_path = os.path.join(pipeline_root, 'metadata', 'metadata.db')
    self._metadata_path = metadata_path
    connection_config = metadata.sqlite_metadata_connection_config(
        metadata_path
    )
    connection_config.sqlite.SetInParent()
    self._mlmd_connection = metadata.Metadata(
        connection_config=connection_config
    )

  def test_new_pipeline_state(self):
    with self._mlmd_connection as m:
      pstate._active_owned_pipelines_exist = False
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pipeline_state = pstate.PipelineState.new(m, pipeline)

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      with pipeline_state:
        self.assertProtoPartiallyEquals(
            mlmd_executions[0],
            pipeline_state._execution,
            ignored_fields=[
                'create_time_since_epoch',
                'last_update_time_since_epoch',
            ],
        )

      self.assertEqual(pipeline, pipeline_state.pipeline)
      self.assertEqual(
          task_lib.PipelineUid.from_pipeline(pipeline),
          pipeline_state.pipeline_uid,
      )
      self.assertTrue(pstate._active_owned_pipelines_exist)

  def test_new_pipeline_state_with_sub_pipelines(self):
    with self._mlmd_connection as m:
      pstate._active_owned_pipelines_exist = False
      pipeline = _test_pipeline('pipeline1')
      # Add 2 additional layers of sub pipelines. Note that there is no normal
      # pipeline node in the first pipeline layer.
      _add_sub_pipeline(
          pipeline,
          'sub_pipeline1',
          sub_pipeline_nodes=['Trainer'],
          sub_pipeline_run_id='sub_pipeline1_run0',
      )
      _add_sub_pipeline(
          pipeline.nodes[0].sub_pipeline,
          'sub_pipeline2',
          sub_pipeline_nodes=['Trainer'],
          sub_pipeline_run_id='sub_pipeline1_sub_pipeline2_run0',
      )
      pipeline_state = pstate.PipelineState.new(m, pipeline)

      # Altogether 2 pipeline run contexts are registered. Sub pipeline 2 run
      # context is not reigstered because the recursion stops once it finds the
      # the first normal pipeline node.
      self.assertLen(m.store.get_contexts_by_type(type_name='pipeline_run'), 2)
      run_context = m.store.get_context_by_type_and_name(
          type_name='pipeline_run', context_name='run0'
      )
      self.assertIsNotNone(run_context)
      sub_pipeline_run_context = m.store.get_context_by_type_and_name(
          type_name='pipeline_run', context_name='sub_pipeline1_run0'
      )
      self.assertIsNotNone(sub_pipeline_run_context)
      with pipeline_state:
        self.assertProtoPartiallyEquals(
            run_context,
            mlmd.proto.Context(
                id=run_context.id,
                type_id=run_context.type_id,
                name='run0',
                type='pipeline_run',
            ),
            ignored_fields=[
                'create_time_since_epoch',
                'last_update_time_since_epoch',
            ],
        )

        self.assertProtoPartiallyEquals(
            sub_pipeline_run_context,
            mlmd.proto.Context(
                id=sub_pipeline_run_context.id,
                type_id=sub_pipeline_run_context.type_id,
                name='sub_pipeline1_run0',
                type='pipeline_run',
            ),
            ignored_fields=[
                'create_time_since_epoch',
                'last_update_time_since_epoch',
            ],
        )

  def test_load_pipeline_state(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pstate.PipelineState.new(m, pipeline)

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        self.assertProtoPartiallyEquals(
            mlmd_executions[0], pipeline_state._execution
        )

      self.assertEqual(pipeline, pipeline_state.pipeline)
      self.assertEqual(
          task_lib.PipelineUid.from_pipeline(pipeline),
          pipeline_state.pipeline_uid,
      )

  @mock.patch.object(pstate, '_get_pipeline_from_orchestrator_execution')
  def test_load_pipeline_state_with_execution(
      self, mock_get_pipeline_from_orchestrator_execution
  ):
    mock_get_pipeline_from_orchestrator_execution.side_effect = (
        fileio.NotFoundError()
    )
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pstate.PipelineState.new(m, pipeline)

      pipeline_state = pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      )

      self.assertIsNotNone(pipeline_state.pipeline_decode_error)
      self.assertEqual(pipeline_state.pipeline.ByteSize(), 0)

  def test_load_all_active_pipeline_state_flag_false(self):
    # no MLMD calls when there _active_owned_pipelines_exist is False.
    mock_store = mock.create_autospec(mlmd.MetadataStore)
    self._mlmd_connection._store = mock_store
    _ = self.enter_context(
        mock.patch.object(mlmd, 'MetadataStore', autospec=True)
    )

    pstate._active_owned_pipelines_exist = False
    pipeline_states = pstate.PipelineState.load_all_active_and_owned(
        self._mlmd_connection
    )
    self.assertEmpty(pipeline_states)
    mock_store.get_executions_by_context.assert_not_called()
    mock_store.get_contexts_by_type.assert_not_called()
    self.assertFalse(pstate._active_owned_pipelines_exist)

  def test_load_all_active_pipeline_state_active_pipelines(self):
    with self._mlmd_connection as m:
      execution_mock = self.enter_context(
          mock.patch.object(
              mlmd.MetadataStore,
              'get_executions_by_context',
              wraps=m.store.get_executions_by_context,
          )
      )
      context_mock = self.enter_context(
          mock.patch.object(
              mlmd.MetadataStore,
              'get_contexts_by_type',
              wraps=m.store.get_contexts_by_type,
          )
      )
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pstate.PipelineState.new(m, pipeline)
      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)
      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)

      pipeline_states = pstate.PipelineState.load_all_active_and_owned(m)
      self.assertLen(pipeline_states, 1)
      execution_mock.assert_called()
      context_mock.assert_called()
      self.assertTrue(pstate._active_owned_pipelines_exist)

  def test_load_all_active_pipeline_state_no_active_pipelines(self):
    pstate._active_owned_pipelines_exist = True
    mock_store = mock.create_autospec(mlmd.MetadataStore)
    self._mlmd_connection._store = mock_store
    _ = self.enter_context(
        mock.patch.object(mlmd, 'MetadataStore', autospec=True)
    )
    mock_store.get_executions_by_context.return_value = []
    mock_store.get_contexts_by_type.return_value = [
        metadata_store_pb2.Context(
            id=1, type_id=11, name='pipeline1', type='__ORCHESTRATOR__'
        )
    ]
    pipeline_states = pstate.PipelineState.load_all_active_and_owned(
        self._mlmd_connection
    )
    self.assertEmpty(pipeline_states, 0)
    mock_store.get_contexts_by_type.assert_called_once()
    mock_store.get_executions_by_context.assert_called_once()
    self.assertFalse(pstate._active_owned_pipelines_exist)

  def load_pipeline_state_by_run(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pstate.PipelineState.new(m, pipeline)

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      with pstate.PipelineState.load_run(
          m,
          pipeline_id=pipeline.pipeline_info.id,
          run_id=pipeline.runtime_spec.pipeline_run_id.field_value.string_value,
      ) as pipeline_state:
        self.assertProtoPartiallyEquals(
            mlmd_executions[0], pipeline_state._execution
        )

  @mock.patch.object(pstate, 'get_all_node_executions')
  @mock.patch.object(execution_lib, 'get_output_artifacts')
  def test_get_all_node_artifacts(
      self, mock_get_output_artifacts, mock_get_all_pipeline_executions
  ):
    artifact = metadata_store_pb2.Artifact(id=1)
    artifact_obj = mock.Mock()
    artifact_obj.mlmd_artifact = artifact
    with self._mlmd_connection as m:
      mock_get_output_artifacts.return_value = {'key': [artifact_obj]}
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      mock_get_all_pipeline_executions.return_value = {
          pipeline.nodes[0].pipeline_node.node_info.id: [
              metadata_store_pb2.Execution(id=1)
          ]
      }
      self.assertEqual(
          {
              pipeline.nodes[0].pipeline_node.node_info.id: {
                  1: {'key': [artifact]}
              }
          },
          pstate.get_all_node_artifacts(pipeline, m),
      )

  @mock.patch.object(pstate, 'get_all_node_executions', autospec=True)
  @mock.patch.object(execution_lib, 'get_output_artifacts', autospec=True)
  def test_get_all_node_artifacts_with_execution_filter_options(
      self, mock_get_output_artifacts, mock_get_all_node_executions
  ):
    artifact_1 = metadata_store_pb2.Artifact(id=1)
    artifact_2 = metadata_store_pb2.Artifact(id=2)

    artifact_obj_1 = mock.Mock()
    artifact_obj_1.mlmd_artifact = artifact_1
    artifact_obj_2 = mock.Mock()
    artifact_obj_2.mlmd_artifact = artifact_2

    create_time_1 = 1234567891012
    create_time_2 = 1234567891013
    execution_1 = metadata_store_pb2.Execution(
        id=1,
        type='test_execution_type1',
        create_time_since_epoch=create_time_1,
    )
    execution_2 = metadata_store_pb2.Execution(
        id=2,
        type='test_execution_type2',
        create_time_since_epoch=create_time_2,
    )

    with self._mlmd_connection as mlmd_handle:
      # Expect node `Trainer` to be associated with 2 executions:
      # `execution_1` outputs `artifact_1`,
      # `execution_2` outputs `artifact_2`.
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      mock_get_all_node_executions.return_value = {
          pipeline.nodes[0].pipeline_node.node_info.id: [
              execution_1,
              execution_2,
          ]
      }
      # Expect get_output_artifacts() to be called twice.
      mock_get_output_artifacts.side_effect = [
          {'key1': [artifact_obj_1]},
          {'key2': [artifact_obj_2]},
      ]

      execution_filter_options = metadata_pb2.NodeFilterOptions(
          types=['test_execution_type1', 'test_execution_type2'],
      )
      execution_filter_options.min_create_time.FromMilliseconds(create_time_1)
      execution_filter_options.max_create_time.FromMilliseconds(create_time_2)
      self.assertEqual(
          {
              pipeline.nodes[0].pipeline_node.node_info.id: {
                  1: {'key1': [artifact_1]},
                  2: {'key2': [artifact_2]},
              }
          },
          pstate.get_all_node_artifacts(
              pipeline,
              mlmd_handle,
              execution_filter_options=execution_filter_options,
          ),
      )

      mock_get_all_node_executions.assert_called_once_with(
          mock.ANY,
          mock.ANY,
          node_filter_options=execution_filter_options,
      )
      # Assert `execution_filter_options` is called twice with proper execution
      # ids.
      self.assertSequenceEqual(
          (mock.call(mock.ANY, 1), mock.call(mock.ANY, 2)),
          mock_get_output_artifacts.mock_calls,
      )

  @mock.patch.object(task_gen_utils, 'get_executions')
  def test_get_all_node_executions(self, mock_get_executions):
    execution = metadata_store_pb2.Execution(name='test_execution')
    mock_get_executions.return_value = [execution]
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      self.assertEqual(
          {pipeline.nodes[0].pipeline_node.node_info.id: [execution]},
          pstate.get_all_node_executions(pipeline, m),
      )
      mock_get_executions.assert_called_once_with(
          mock.ANY, mock.ANY, additional_filters=None
      )

  @mock.patch.object(task_gen_utils, 'get_executions')
  def test_get_all_node_executions_with_node_filter_options(
      self, mock_get_executions
  ):
    execution_1 = metadata_store_pb2.Execution(
        name='test_execution',
        type='test_execution_type1',
        create_time_since_epoch=1234567891012,
    )
    execution_2 = metadata_store_pb2.Execution(
        name='test_execution',
        type='test_execution_type2',
        create_time_since_epoch=1234567891013,
    )
    mock_get_executions.return_value = [execution_1, execution_2]

    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])

      node_filter_options = metadata_pb2.NodeFilterOptions(
          types=['test_execution_type1', 'test_execution_type2'],
      )
      node_filter_options.min_create_time.FromMilliseconds(1234567891012)
      node_filter_options.max_create_time.FromMilliseconds(1234567891013)

      self.assertEqual(
          {
              pipeline.nodes[0].pipeline_node.node_info.id: [
                  execution_1,
                  execution_2,
              ]
          },
          pstate.get_all_node_executions(pipeline, m, node_filter_options),
      )

      mock_get_executions.assert_called_once_with(
          mock.ANY,
          mock.ANY,
          additional_filters=[
              'create_time_since_epoch <= 1234567891013',
              'create_time_since_epoch >= 1234567891012',
              'type IN ("test_execution_type1","test_execution_type2")',
          ],
      )

  def test_new_pipeline_state_when_pipeline_already_exists(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          pipeline_nodes=['Trainer'],
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_run_id='run0',
      )
      pipeline_state = pstate.PipelineState.new(m, pipeline)
      self.assertEqual(
          task_lib.PipelineUid(pipeline_id='pipeline1', pipeline_run_id='run0'),
          pipeline_state.pipeline_uid,
      )

      # New run should be prohibited even if run id is different.
      pipeline.runtime_spec.pipeline_run_id.field_value.string_value = 'run1'
      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        pstate.PipelineState.new(m, pipeline)
      self.assertEqual(
          status_lib.Code.ALREADY_EXISTS, exception_context.exception.code
      )

  def test_new_pipeline_state_when_pipeline_already_exists_concurrent_runs_enabled(
      self,
  ):
    with test_utils.concurrent_pipeline_runs_enabled_env():
      with self._mlmd_connection as m:
        pipeline = _test_pipeline(
            'pipeline1',
            pipeline_nodes=['Trainer'],
            execution_mode=pipeline_pb2.Pipeline.SYNC,
            pipeline_run_id='run0',
        )
        pipeline_state = pstate.PipelineState.new(m, pipeline)
        self.assertEqual(
            task_lib.PipelineUid(
                pipeline_id='pipeline1', pipeline_run_id='run0'
            ),
            pipeline_state.pipeline_uid,
        )

        # New run should be allowed if run id is different.
        pipeline.runtime_spec.pipeline_run_id.field_value.string_value = 'run1'
        pipeline_state = pstate.PipelineState.new(m, pipeline)
        self.assertEqual(
            task_lib.PipelineUid(
                pipeline_id='pipeline1', pipeline_run_id='run1'
            ),
            pipeline_state.pipeline_uid,
        )

        # New run should be prohibited if run id is same.
        with self.assertRaises(
            status_lib.StatusNotOkError
        ) as exception_context:
          pstate.PipelineState.new(m, pipeline)
        self.assertEqual(
            status_lib.Code.ALREADY_EXISTS, exception_context.exception.code
        )

  def test_load_pipeline_state_when_no_active_pipeline(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)

      # No such pipeline so NOT_FOUND error should be raised.
      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        pstate.PipelineState.load(m, pipeline_uid)
      self.assertEqual(
          status_lib.Code.NOT_FOUND, exception_context.exception.code
      )

      pipeline_state = pstate.PipelineState.new(m, pipeline)

      # No error as there's an active pipeline.
      pstate.PipelineState.load(m, pipeline_uid)

      # Inactivate the pipeline.
      with pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )

      # No active pipeline so NOT_FOUND error should be raised.
      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        with pstate.PipelineState.load(m, pipeline_uid):
          pass
      self.assertEqual(
          status_lib.Code.NOT_FOUND, exception_context.exception.code
      )

  def test_pipeline_stop_initiation(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        self.assertIsNone(pipeline_state.stop_initiated_reason())
        status = status_lib.Status(
            code=status_lib.Code.CANCELLED, message='foo bar'
        )
        pipeline_state.initiate_stop(status)
        self.assertEqual(status, pipeline_state.stop_initiated_reason())

      # Reload from MLMD and verify.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        self.assertEqual(status, pipeline_state.stop_initiated_reason())

  def test_pipeline_resume_initiation(self):
    with self._mlmd_connection as m:
      pstate._active_owned_pipelines_exist = False
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        self.assertIsNone(pipeline_state.stop_initiated_reason())
        status = status_lib.Status(
            code=status_lib.Code.CANCELLED, message='foo bar'
        )
        pipeline_state.initiate_stop(status)
        self.assertEqual(status, pipeline_state.stop_initiated_reason())
        pipeline_state.initiate_resume()

      self.assertTrue(pstate._active_owned_pipelines_exist)

      # Reload from MLMD and verify.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        self.assertIsNone(pipeline_state.stop_initiated_reason())

  def test_update_initiation_and_apply(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1', param=1, pipeline_nodes=['Trainer']
      )
      updated_pipeline = _test_pipeline(
          'pipeline1', param=2, pipeline_nodes=['Trainer']
      )

      # Initiate pipeline update.
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        self.assertFalse(pipeline_state.is_update_initiated())
        pipeline_state.initiate_update(
            updated_pipeline, pipeline_pb2.UpdateOptions()
        )
        self.assertTrue(pipeline_state.is_update_initiated())

      # Reload from MLMD and verify update initiation followed by applying the
      # pipeline update.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        self.assertTrue(pipeline_state.is_update_initiated())
        self.assertEqual(pipeline, pipeline_state.pipeline)
        pipeline_state.apply_pipeline_update()
        # Verify in-memory state after update application.
        self.assertFalse(pipeline_state.is_update_initiated())
        self.assertTrue(pipeline_state.is_active())
        self.assertEqual(updated_pipeline, pipeline_state.pipeline)

      # Reload from MLMD and verify update application was correctly persisted.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        self.assertFalse(pipeline_state.is_update_initiated())
        self.assertTrue(pipeline_state.is_active())
        self.assertEqual(updated_pipeline, pipeline_state.pipeline)

      # Update should fail if execution mode is different.
      updated_pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['Trainer'],
      )
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        with self.assertRaisesRegex(
            status_lib.StatusNotOkError,
            'Updating execution_mode.*not supported',
        ):
          pipeline_state.initiate_update(
              updated_pipeline, pipeline_pb2.UpdateOptions()
          )

      # Update should fail if pipeline structure changed.
      updated_pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['Trainer', 'Evaluator'],
      )
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        with self.assertRaisesRegex(
            status_lib.StatusNotOkError,
            'Updating execution_mode.*not supported',
        ):
          pipeline_state.initiate_update(
              updated_pipeline, pipeline_pb2.UpdateOptions()
          )

  @mock.patch.object(pstate, 'time')
  def test_initiate_node_start_stop(self, mock_time):
    mock_time.time.return_value = time.time()
    events = []

    def recorder(event):
      events.append(event)

    with TestEnv(None, 2000), event_observer.init(), self._mlmd_connection as m:
      event_observer.register_observer(recorder)

      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      node_uid = task_lib.NodeUid(node_id='Trainer', pipeline_uid=pipeline_uid)
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(node_uid) as node_state:
          node_state.update(pstate.NodeState.STARTED)
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STARTED, node_state.state)

      # Reload from MLMD and verify node is started.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STARTED, node_state.state)

        # Set node state to STOPPING.
        status = status_lib.Status(
            code=status_lib.Code.ABORTED, message='foo bar'
        )
        with pipeline_state.node_state_update_context(node_uid) as node_state:
          node_state.update(pstate.NodeState.STOPPING, status)
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STOPPING, node_state.state)
        self.assertEqual(status, node_state.status)

      # Reload from MLMD and verify node is stopped.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STOPPING, node_state.state)
        self.assertEqual(status, node_state.status)

        # Set node state to STARTED.
        with pipeline_state.node_state_update_context(node_uid) as node_state:
          node_state.update(pstate.NodeState.STARTED)
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STARTED, node_state.state)

      # Reload from MLMD and verify node is started.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        node_state = pipeline_state.get_node_state(node_uid)
        self.assertEqual(pstate.NodeState.STARTED, node_state.state)

      event_observer.testonly_wait()

      want = [
          event_observer.PipelineStarted(
              pipeline_state=None, pipeline_uid=pipeline_uid
          ),
          event_observer.NodeStateChange(
              execution=None,
              pipeline_uid=pipeline_uid,
              pipeline_run=None,
              node_id='Trainer',
              old_state=pstate.NodeState(
                  state='started',
              ),
              new_state=pstate.NodeState(
                  state='stopping',
                  status_code=status_lib.Code.ABORTED,
                  status_msg='foo bar',
                  state_history=[
                      pstate.StateRecord(
                          state=pstate.NodeState.STARTED,
                          backfill_token='',
                          status_code=None,
                          update_time=mock_time.time.return_value,
                      ),
                  ],
              ),
          ),
          event_observer.NodeStateChange(
              execution=None,
              pipeline_uid=pipeline_uid,
              pipeline_run=None,
              node_id='Trainer',
              old_state=pstate.NodeState(
                  state='stopping',
                  status_code=status_lib.Code.ABORTED,
                  status_msg='foo bar',
                  state_history=[
                      pstate.StateRecord(
                          state=pstate.NodeState.STARTED,
                          backfill_token='',
                          status_code=None,
                          update_time=mock_time.time.return_value,
                      ),
                  ],
              ),
              new_state=pstate.NodeState(
                  state='started',
                  state_history=[
                      pstate.StateRecord(
                          state=pstate.NodeState.STARTED,
                          backfill_token='',
                          status_code=None,
                          update_time=mock_time.time.return_value,
                      ),
                      pstate.StateRecord(
                          state=pstate.NodeState.STOPPING,
                          backfill_token='',
                          status_code=status_lib.Code.ABORTED,
                          update_time=mock_time.time.return_value,
                      ),
                  ],
              ),
          ),
      ]
      # Set execution / pipeline_state to None, so we don't compare those fields
      got = []
      for x in events:
        r = x
        if hasattr(x, 'execution'):
          r = dataclasses.replace(r, execution=None)
        if hasattr(x, 'pipeline_state'):
          r = dataclasses.replace(r, pipeline_state=None)
        got.append(r)

      self.assertListEqual(want, got)

  @mock.patch.object(pstate, 'time')
  def test_get_node_states_dict(self, mock_time):
    mock_time.time.return_value = time.time()
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen', 'Transform', 'Trainer', 'Evaluator'],
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      eg_node_uid = task_lib.NodeUid(pipeline_uid, 'ExampleGen')
      transform_node_uid = task_lib.NodeUid(pipeline_uid, 'Transform')
      trainer_node_uid = task_lib.NodeUid(pipeline_uid, 'Trainer')
      evaluator_node_uid = task_lib.NodeUid(pipeline_uid, 'Evaluator')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.COMPLETE)
        with pipeline_state.node_state_update_context(
            transform_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.RUNNING)
        with pipeline_state.node_state_update_context(
            trainer_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.STARTED)
      with pstate.PipelineState.load(m, pipeline_uid) as pipeline_state:
        self.assertEqual(
            {
                eg_node_uid: pstate.NodeState(
                    state=pstate.NodeState.COMPLETE,
                    state_history=[
                        pstate.StateRecord(
                            state=pstate.NodeState.STARTED,
                            backfill_token='',
                            status_code=None,
                            update_time=mock_time.time.return_value,
                        )
                    ],
                ),
                transform_node_uid: pstate.NodeState(
                    state=pstate.NodeState.RUNNING,
                    state_history=[
                        pstate.StateRecord(
                            backfill_token='',
                            state=pstate.NodeState.STARTED,
                            status_code=None,
                            update_time=mock_time.time.return_value,
                        )
                    ],
                ),
                trainer_node_uid: pstate.NodeState(
                    state=pstate.NodeState.STARTED,
                ),
                evaluator_node_uid: pstate.NodeState(
                    state=pstate.NodeState.STARTED
                ),
            },
            pipeline_state.get_node_states_dict(),
        )

  @parameterized.named_parameters(
      ('string', 'string_value'),
      ('int', 1),
      ('float', 2.3),
  )
  def test_save_and_read_and_remove_property(self, property_value):
    property_key = 'key'
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pipeline_state.save_property(property_key, property_value)

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      self.assertIsNotNone(
          mlmd_executions[0].custom_properties.get(property_key)
      )
      self.assertEqual(
          data_types_utils.get_metadata_value(
              mlmd_executions[0].custom_properties[property_key]
          ),
          property_value,
      )

      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)
      ) as pipeline_state:
        # Also check that PipelineState returns the correct value
        self.assertEqual(
            pipeline_state.get_property(property_key), property_value
        )
        pipeline_state.remove_property(property_key)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      self.assertIsNone(mlmd_executions[0].custom_properties.get(property_key))

  def test_get_orchestration_options(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline', pipeline_nodes=['Trainer'])
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        options = pipeline_state.get_orchestration_options()
        self.assertFalse(options.fail_fast)

  def test_async_pipeline_views(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1', pipeline_nodes=['Trainer'])
      with pstate.PipelineState.new(
          m, pipeline, {'foo': 1, 'bar': 'baz'}
      ) as pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )

      views = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      self.assertLen(views, 1)
      self.assertProtoEquals(pipeline, views[0].pipeline)
      self.assertEqual({'foo': 1, 'bar': 'baz'}, views[0].pipeline_run_metadata)

      pstate.PipelineState.new(m, pipeline)
      views = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      self.assertLen(views, 2)
      self.assertProtoEquals(pipeline, views[0].pipeline)
      self.assertProtoEquals(pipeline, views[1].pipeline)

  def test_sync_pipeline_views(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_run_id='001',
          pipeline_nodes=['Trainer'],
      )
      with self.assertRaises(status_lib.StatusNotOkError):
        pstate.PipelineView.load(m, pipeline.pipeline_info.id)
      with pstate.PipelineState.new(
          m, pipeline, {'foo': 1, 'bar': 'baz'}
      ) as pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )
        pipeline_state.initiate_stop(
            status_lib.Status(code=status_lib.Code.CANCELLED, message='msg')
        )

      views = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      self.assertLen(views, 1)
      self.assertEqual(views[0].pipeline_run_id, '001')
      self.assertEqual(
          views[0].pipeline_status_code,
          run_state_pb2.RunState.StatusCodeValue(
              value=status_lib.Code.CANCELLED
          ),
      )
      self.assertEqual(views[0].pipeline_status_message, 'msg')
      self.assertEqual({'foo': 1, 'bar': 'baz'}, views[0].pipeline_run_metadata)
      self.assertProtoEquals(pipeline, views[0].pipeline)

      pipeline2 = _test_pipeline(
          'pipeline',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_run_id='002',
          pipeline_nodes=['Trainer'],
      )
      pstate.PipelineState.new(m, pipeline2)

      views = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      self.assertLen(views, 2)
      views_dict = {view.pipeline_run_id: view for view in views}
      self.assertCountEqual(['001', '002'], views_dict.keys())
      self.assertProtoEquals(pipeline, views_dict['001'].pipeline)
      self.assertProtoEquals(pipeline2, views_dict['002'].pipeline)
      views_status_messages = {view.pipeline_status_message for view in views}
      self.assertEqual(views_status_messages, {'', 'msg'})

      view1 = pstate.PipelineView.load(m, pipeline.pipeline_info.id, '001')
      view2 = pstate.PipelineView.load(m, pipeline.pipeline_info.id, '002')
      latest_view = pstate.PipelineView.load(m, pipeline.pipeline_info.id)
      latest_non_active_view = pstate.PipelineView.load(
          m, pipeline.pipeline_info.id, non_active_only=True
      )
      self.assertProtoEquals(pipeline, view1.pipeline)
      self.assertProtoEquals(pipeline2, view2.pipeline)
      self.assertProtoEquals(pipeline2, latest_view.pipeline)
      self.assertProtoEquals(pipeline, latest_non_active_view.pipeline)

  @mock.patch.object(pstate, 'time')
  def test_pipeline_view_get_pipeline_run_state(self, mock_time):
    mock_time.time.return_value = 5
    with self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1', pipeline_pb2.Pipeline.SYNC, pipeline_nodes=['Trainer']
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)

      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.RUNNING
        )
      [view] = pstate.PipelineView.load_all(m, pipeline_uid.pipeline_id)
      self.assertProtoPartiallyEquals(
          run_state_pb2.RunState(state=run_state_pb2.RunState.RUNNING),
          view.get_pipeline_run_state(),
          ignored_fields=['update_time'],
      )

      with pstate.PipelineState.load(m, pipeline_uid) as pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )
      [view] = pstate.PipelineView.load_all(m, pipeline_uid.pipeline_id)
      self.assertProtoPartiallyEquals(
          run_state_pb2.RunState(state=run_state_pb2.RunState.COMPLETE),
          view.get_pipeline_run_state(),
          ignored_fields=['update_time'],
      )

  @mock.patch.object(pstate, 'time')
  def test_pipeline_view_get_node_run_states(self, mock_time):
    mock_time.time.return_value = time.time()
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=[
              'ExampleGen',
              'Transform',
              'Trainer',
              'Evaluator',
              'Pusher',
          ],
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      eg_node_uid = task_lib.NodeUid(pipeline_uid, 'ExampleGen')
      transform_node_uid = task_lib.NodeUid(pipeline_uid, 'Transform')
      trainer_node_uid = task_lib.NodeUid(pipeline_uid, 'Trainer')
      evaluator_node_uid = task_lib.NodeUid(pipeline_uid, 'Evaluator')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.RUNNING)
        with pipeline_state.node_state_update_context(
            transform_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.STARTED)
        with pipeline_state.node_state_update_context(
            trainer_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.STARTED)
        with pipeline_state.node_state_update_context(
            evaluator_node_uid
        ) as node_state:
          node_state.update(
              pstate.NodeState.FAILED,
              status_lib.Status(
                  code=status_lib.Code.ABORTED, message='foobar error'
              ),
          )

      [view] = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      run_states_dict = view.get_node_run_states()
      self.assertEqual(
          run_state_pb2.RunState(
              state=run_state_pb2.RunState.RUNNING,
              update_time=int(mock_time.time.return_value * 1000),
          ),
          run_states_dict['ExampleGen'],
      )
      self.assertEqual(
          run_state_pb2.RunState(
              state=run_state_pb2.RunState.READY,
              update_time=int(mock_time.time.return_value * 1000),
          ),
          run_states_dict['Transform'],
      )
      self.assertEqual(
          run_state_pb2.RunState(
              state=run_state_pb2.RunState.READY,
              update_time=int(mock_time.time.return_value * 1000),
          ),
          run_states_dict['Trainer'],
      )
      self.assertEqual(
          run_state_pb2.RunState(
              state=run_state_pb2.RunState.FAILED,
              status_code=run_state_pb2.RunState.StatusCodeValue(
                  value=status_lib.Code.ABORTED
              ),
              status_msg='foobar error',
              update_time=int(mock_time.time.return_value * 1000),
          ),
          run_states_dict['Evaluator'],
      )
      self.assertEqual(
          run_state_pb2.RunState(
              state=run_state_pb2.RunState.READY,
              update_time=int(mock_time.time.return_value * 1000),
          ),
          run_states_dict['Pusher'],
      )

  @mock.patch.object(pstate, 'time')
  def test_pipeline_view_get_node_run_state_history(self, mock_time):
    mock_time.time.return_value = time.time()
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen'],
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      eg_node_uid = task_lib.NodeUid(pipeline_uid, 'ExampleGen')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.RUNNING)
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.COMPLETE)

      [view] = pstate.PipelineView.load_all(m, pipeline.pipeline_info.id)
      run_state_history = view.get_node_run_states_history()

      self.assertEqual(
          {
              'ExampleGen': [
                  (
                      run_state_pb2.RunState(
                          state=run_state_pb2.RunState.READY,
                          update_time=int(mock_time.time.return_value * 1000),
                      )
                  ),
                  (
                      run_state_pb2.RunState(
                          state=run_state_pb2.RunState.RUNNING,
                          update_time=int(mock_time.time.return_value * 1000),
                      )
                  ),
              ]
          },
          run_state_history,
      )

  @mock.patch.object(pstate, 'time')
  def test_node_state_for_skipped_nodes_in_partial_pipeline_run(
      self, mock_time
  ):
    """Tests that nodes marked to be skipped have the right node state and previous node state."""
    mock_time.time.return_value = time.time()
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen', 'Transform', 'Trainer'],
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      eg_node_uid = task_lib.NodeUid(pipeline_uid, 'ExampleGen')
      transform_node_uid = task_lib.NodeUid(pipeline_uid, 'Transform')
      trainer_node_uid = task_lib.NodeUid(pipeline_uid, 'Trainer')

      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.COMPLETE)
        with pipeline_state.node_state_update_context(
            trainer_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.FAILED)
        with pipeline_state.node_state_update_context(
            transform_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.FAILED)
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )

      [latest_pipeline_view] = pstate.PipelineView.load_all(
          m, pipeline.pipeline_info.id
      )

      # Mark ExampleGen and Transform to be skipped.
      pipeline.nodes[0].pipeline_node.execution_options.skip.SetInParent()
      pipeline.nodes[1].pipeline_node.execution_options.skip.SetInParent()
      pstate.PipelineState.new(
          m, pipeline, reused_pipeline_view=latest_pipeline_view
      )
      with pstate.PipelineState.load(m, pipeline_uid) as pipeline_state:
        self.assertEqual(
            {
                eg_node_uid: pstate.NodeState(
                    state=pstate.NodeState.SKIPPED_PARTIAL_RUN,
                    last_updated_time=mock_time.time.return_value,
                ),
                transform_node_uid: pstate.NodeState(
                    state=pstate.NodeState.SKIPPED_PARTIAL_RUN,
                    last_updated_time=mock_time.time.return_value,
                ),
                trainer_node_uid: pstate.NodeState(
                    state=pstate.NodeState.STARTED,
                    last_updated_time=mock_time.time.return_value,
                ),
            },
            pipeline_state.get_node_states_dict(),
        )
        self.assertEqual(
            {
                eg_node_uid: pstate.NodeState(
                    state=pstate.NodeState.COMPLETE,
                    state_history=[
                        pstate.StateRecord(
                            state=pstate.NodeState.STARTED,
                            backfill_token='',
                            status_code=None,
                            update_time=mock_time.time.return_value,
                        )
                    ],
                ),
                transform_node_uid: pstate.NodeState(
                    state=pstate.NodeState.FAILED,
                    state_history=[
                        pstate.StateRecord(
                            state=pstate.NodeState.STARTED,
                            backfill_token='',
                            status_code=None,
                            update_time=mock_time.time.return_value,
                        )
                    ],
                ),
            },
            pipeline_state.get_previous_node_states_dict(),
        )

  def test_load_all_with_list_options(self):
    """Verifies list_options parameter is applied to MLMD calls in load_all."""
    with self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_run_id='001',
          pipeline_nodes=['Trainer'],
      )
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )
      pipeline2 = _test_pipeline(
          'pipeline',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_run_id='002',
          pipeline_nodes=['Trainer'],
      )
      pstate.PipelineState.new(m, pipeline2)
      list_options = mlmd.ListOptions(
          filter_query='custom_properties.pipeline_run_id.string_value = "001"'
      )

      pipeline_runs = pstate.PipelineView.load_all(
          m, 'pipeline', list_options=list_options
      )

      self.assertLen(pipeline_runs, 1)
      self.assertEqual(pipeline_runs[0].pipeline_run_id, '001')

  @mock.patch.object(pstate, 'time')
  def test_get_previous_node_run_states_for_skipped_nodes(self, mock_time):
    """Tests that nodes marked to be skipped have the right previous run state."""
    mock_time.time.return_value = time.time()
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen', 'Transform', 'Trainer', 'Pusher'],
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      eg_node_uid = task_lib.NodeUid(pipeline_uid, 'ExampleGen')
      transform_node_uid = task_lib.NodeUid(pipeline_uid, 'Transform')
      trainer_node_uid = task_lib.NodeUid(pipeline_uid, 'Trainer')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        with pipeline_state.node_state_update_context(
            eg_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.FAILED)
        with pipeline_state.node_state_update_context(
            transform_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.RUNNING)
        with pipeline_state.node_state_update_context(
            trainer_node_uid
        ) as node_state:
          node_state.update(pstate.NodeState.STARTED)
        pipeline_state.set_pipeline_execution_state(
            metadata_store_pb2.Execution.COMPLETE
        )

      view_run_0 = pstate.PipelineView.load(
          m, pipeline.pipeline_info.id, 'run0'
      )
      self.assertEmpty(view_run_0.get_previous_node_run_states())

      # Mark ExampleGen and Transform to be skipped.
      pipeline.runtime_spec.pipeline_run_id.field_value.string_value = 'run1'
      pipeline.nodes[0].pipeline_node.execution_options.skip.SetInParent()
      pipeline.nodes[1].pipeline_node.execution_options.skip.SetInParent()
      pstate.PipelineState.new(m, pipeline, reused_pipeline_view=view_run_0)
      view_run_1 = pstate.PipelineView.load(
          m, pipeline.pipeline_info.id, 'run1'
      )
      self.assertEqual(
          {
              'ExampleGen': run_state_pb2.RunState(
                  state=run_state_pb2.RunState.FAILED,
                  update_time=int(mock_time.time.return_value * 1000),
              ),
              'Transform': run_state_pb2.RunState(
                  state=run_state_pb2.RunState.RUNNING,
                  update_time=int(mock_time.time.return_value * 1000),
              ),
          },
          view_run_1.get_previous_node_run_states(),
      )

    self.assertEqual(
        {
            'ExampleGen': [
                run_state_pb2.RunState(
                    state=run_state_pb2.RunState.READY,
                    update_time=int(mock_time.time.return_value * 1000),
                )
            ],
            'Transform': [
                run_state_pb2.RunState(
                    state=run_state_pb2.RunState.READY,
                    update_time=int(mock_time.time.return_value * 1000),
                )
            ],
        },
        view_run_1.get_previous_node_run_states_history(),
    )

  def test_create_and_load_concurrent_pipeline_runs(self):
    with test_utils.concurrent_pipeline_runs_enabled_env():
      with self._mlmd_connection as m:
        pipeline_run0 = _test_pipeline(
            'pipeline1',
            pipeline_run_id='run0',
            execution_mode=pipeline_pb2.Pipeline.SYNC,
            pipeline_nodes=['ExampleGen', 'Trainer'],
        )
        pipeline_run1 = _test_pipeline(
            'pipeline1',
            pipeline_run_id='run1',
            execution_mode=pipeline_pb2.Pipeline.SYNC,
            pipeline_nodes=['ExampleGen', 'Transform', 'Trainer'],
        )
        pstate.PipelineState.new(m, pipeline_run0)
        pstate.PipelineState.new(m, pipeline_run1)
        mlmd_contexts = pstate.get_orchestrator_contexts(m)
        self.assertLen(mlmd_contexts, 1)
        mlmd_executions = m.store.get_executions_by_context(
            mlmd_contexts[0].id,
            list_options=mlmd.ListOptions(
                order_by=mlmd.OrderByField.ID, is_asc=True
            ),
        )
        self.assertLen(mlmd_executions, 2)

        with pstate.PipelineState.load(
            m, task_lib.PipelineUid.from_pipeline(pipeline_run0)
        ) as pipeline_state_run0:
          self.assertProtoPartiallyEquals(
              mlmd_executions[0], pipeline_state_run0._execution
          )
        with pstate.PipelineState.load(
            m, task_lib.PipelineUid.from_pipeline(pipeline_run1)
        ) as pipeline_state_run1:
          self.assertProtoPartiallyEquals(
              mlmd_executions[1], pipeline_state_run1._execution
          )
        self.assertEqual(pipeline_run0, pipeline_state_run0.pipeline)
        self.assertEqual(pipeline_run1, pipeline_state_run1.pipeline)
        self.assertEqual(
            task_lib.PipelineUid(
                pipeline_id='pipeline1', pipeline_run_id='run0'
            ),
            pipeline_state_run0.pipeline_uid,
        )
        self.assertEqual(
            task_lib.PipelineUid(
                pipeline_id='pipeline1', pipeline_run_id='run1'
            ),
            pipeline_state_run1.pipeline_uid,
        )

  def test_get_pipeline_and_node(self):
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen', 'Trainer'],
          pipeline_run_id='run0',
      )
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)
      trainer_node_uid = task_lib.NodeUid(pipeline_uid, 'Trainer')
      pstate.PipelineState.new(m, pipeline)
      ir, npv = pstate.get_pipeline_and_node(m, trainer_node_uid, 'run0')
      self.assertEqual(npv.node_info.id, 'Trainer')
      self.assertEqual(
          pipeline.pipeline_info,
          ir.pipeline_info,
      )

  def test_get_pipeline_and_node_not_found(self):
    with TestEnv(None, 20000), self._mlmd_connection as m:
      pipeline = _test_pipeline(
          'pipeline1',
          execution_mode=pipeline_pb2.Pipeline.SYNC,
          pipeline_nodes=['ExampleGen', 'Trainer'],
          pipeline_run_id='run0',
      )
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        node_uid = task_lib.NodeUid(
            pipeline_uid=pipeline_state.pipeline_uid, node_id='NodeDoesNotExist'
        )

      with self.assertRaises(status_lib.StatusNotOkError):
        pstate.get_pipeline_and_node(m, node_uid, 'run0')


class NodeStatesProxyTest(test_utils.TfxTest):

  def setUp(self):
    super().setUp()
    # This is needed because NodeState includes a timestamp at creation.
    self.mock_time = self.enter_context(
        mock.patch.object(pstate, 'time', autospec=True)
    )
    self.mock_time.time.return_value = time.time()

  def test_get_with_invalid_state_type(self):
    proxy = pstate._NodeStatesProxy(metadata_store_pb2.Execution)
    with self.assertRaises(status_lib.StatusNotOkError):
      proxy.get('invalid_state_type')

  def test_get_and_set(self):
    node_states_running = {
        'some_node': pstate.NodeState(
            state=pstate.NodeState.RUNNING,
        )
    }
    node_states_complete = {
        'some_node': pstate.NodeState(
            state=pstate.NodeState.COMPLETE,
        )
    }
    execution = metadata_store_pb2.Execution()
    proxy = pstate._NodeStatesProxy(execution)
    self.assertEmpty(proxy.get())
    proxy.set(node_states_running)
    self.assertEqual(proxy.get(), node_states_running)
    # Underlying execution isn't updated yet.
    self.assertEmpty(execution.custom_properties)
    proxy.set(node_states_complete)
    # Cache is updated even without save().
    self.assertEqual(proxy.get(), node_states_complete)
    proxy.save()
    # Now the underlying execution should be updated.
    self.assertEqual(
        data_types_utils.get_metadata_value(
            execution.custom_properties[pstate._NODE_STATES]
        ),
        json_utils.dumps(node_states_complete),
    )

  def test_save_with_max_str_len(self):
    state_record_1 = pstate.StateRecord(
        state='STARTED',
        backfill_token='token-1',
        update_time=10000,
        status_code=1,
    )
    node_states = {
        'some_node': pstate.NodeState(
            state=pstate.NodeState.COMPLETE, state_history=[state_record_1]
        )
    }
    node_states_without_state_history = {
        'some_node': pstate.NodeState(
            state=pstate.NodeState.COMPLETE,
        )
    }
    with TestEnv(None, 20):
      execution = metadata_store_pb2.Execution()
      proxy = pstate._NodeStatesProxy(execution)
      proxy.set(node_states)
      proxy.save()
      self.assertEqual(
          data_types_utils.get_metadata_value(
              execution.custom_properties[pstate._NODE_STATES]
          ),
          json_utils.dumps(node_states_without_state_history),
      )
    with TestEnv(None, 2000):
      execution = metadata_store_pb2.Execution()
      proxy = pstate._NodeStatesProxy(execution)
      proxy.set(node_states)
      proxy.save()
      self.assertEqual(
          data_types_utils.get_metadata_value(
              execution.custom_properties[pstate._NODE_STATES]
          ),
          json_utils.dumps(node_states),
      )

if __name__ == '__main__':
  tf.test.main()
