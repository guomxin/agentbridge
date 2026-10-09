"""Standalone execution with real authorization transactions and explicit ports."""
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agentbridge.admin.stores import GovernancePolicyStore
from agentbridge.core.capability import CapabilityRegistry, CapabilitySpec
from agentbridge.core.capability_runtime import CapabilityContext, CapabilityRejected, OutcomeUnknown, RequiresUserAction
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.controlled_write_executor import ControlledWriteDependencies, ControlledWriteExecutor
from agentbridge.core.field_submissions import FieldSubmissionStore
from agentbridge.core.task_plan_validation import PlanValidationError
from agentbridge.core.task_plans import TaskPlanStore
from agentbridge.core.tasks import TaskHubStore
from agentbridge.core.user_grants import UserGrants
from agentbridge.core.write_authorizations import WriteAuthorizationStore


class DownstreamContractError(Exception):
    pass


class DownstreamOutcomeError(Exception):
    pass


class ControlledWriteExecutorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'authorization.db'
        self.authorizations = WriteAuthorizationStore(self.db)
        with sqlite3.connect(self.db) as connection:
            connection.execute('CREATE TABLE guard_markers (name TEXT)')
        self.spec = CapabilitySpec(
            name='test.record.save', version='0.1.0', description='Offline test',
            input_schema={}, output_schema={}, effect='controlled_write', adapter='test', workflow='test',
        )
        registry = CapabilityRegistry()
        registry.register(self.spec)
        self.dependencies = ControlledWriteDependencies(
            registry=registry, tasks=Mock(spec=TaskHubStore),
            field_submissions=Mock(spec=FieldSubmissionStore),
            write_authorizations=self.authorizations, user_grants=Mock(spec=UserGrants),
            task_plans=Mock(spec=TaskPlanStore), governance_policies=Mock(spec=GovernancePolicyStore),
            trusted_card_base_url='http://127.0.0.1:8780',
            business_input_interaction=Mock(return_value={'interactionId': 'field-test'}),
            execution_authorization_interaction=Mock(return_value={'interactionId': 'authorization-test'}),
            pending_batch_definition=Mock(), guard_skill_authorization=Mock(),
            validate_task_plan_execution=Mock(),
        )
        self.executor = ControlledWriteExecutor(self.dependencies)
        self.session = dict(user_subject='user-a', system_id='test', session_id='session-a',
                            expected_principal_ref='Alice', downstream_principal_ref='Alice',
                            last_verified_at='2026-10-04T00:00:00Z')
        self.context = CapabilityContext('user-a', 'request-a', 'commit-a', self.spec)
        self.definition = dict(commit_function='offline_commit', outcome_error=DownstreamOutcomeError,
                               contract_error=DownstreamContractError)
        plan = dict(user_subject='user-a', prepare_capability='test.record.save.prepare',
                    resume_arguments={'target': 'record-a'},
                    session_binding={k: v for k, v in self.session.items() if k not in ('user_subject', 'system_id')})
        self.authorization = self.authorizations.create(
            user_subject='user-a', system_id='test', session_id='session-a',
            capability_name=self.spec.name, capability_version=self.spec.version,
            prepare_operation_id='prepare-a', plan=plan, summary={'title': 'Offline test'},
            card_base_url=self.dependencies.trusted_card_base_url,
        )
        self.auth_id = self.authorization['authorization_id']
        csrf = self.authorizations.issue_csrf(self.auth_id)
        self.authorizations.decide(self.auth_id, decision='approve', csrf_token=csrf, csrf_cookie=csrf)
        self.effects = []

    def commit(self, handler=None):
        def successful(_adapter, _worker, _plan, *, enter_commit_boundary):
            enter_commit_boundary()
            self.effects.append('write')
            return {'verified': True}
        with patch('agentbridge.core.controlled_write_executor.resolve_write_function', return_value=handler or successful):
            return self.executor.commit(context=self.context, session=self.session, adapter=object(), worker=object(),
                arguments={'authorization_id': self.auth_id}, prepare_capability='test.record.save.prepare',
                definition=self.definition)

    def markers(self):
        with sqlite3.connect(self.db) as connection:
            return connection.execute('SELECT name FROM guard_markers ORDER BY rowid').fetchall()

    def install_guards(self, *, fail=None):
        connections = []
        def skill(connection, authorization_id, subject):
            self.assertTrue(connection.in_transaction)
            self.assertEqual((authorization_id, subject), (self.auth_id, 'user-a'))
            connections.append(connection)
            connection.execute("INSERT INTO guard_markers VALUES ('skill')")
            if fail == 'skill':
                raise RuntimeError('skill rejected')
        def plan(connection, *, authorization_id, user_subject, operation_id, validate):
            self.assertIs(connection, connections[0])
            self.assertEqual(connection.execute('SELECT name FROM guard_markers').fetchall()[0][0], 'skill')
            self.assertEqual((authorization_id, user_subject, operation_id), (self.auth_id, 'user-a', 'commit-a'))
            connection.execute("INSERT INTO guard_markers VALUES ('plan')")
            validate({'state': 'running'})
            if fail == 'plan':
                raise PlanValidationError('PLAN_NOT_ACTIVE', 'plan rejected')
        self.dependencies.guard_skill_authorization.side_effect = skill
        self.dependencies.task_plans.guard_authorization_consumption.side_effect = plan

    def test_standalone_commit_shares_transaction_and_consumes_once(self):
        self.install_guards()
        self.assertEqual(self.commit(), {'verified': True})
        self.assertEqual(self.markers(), [('skill',), ('plan',)])
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'consumed')
        self.dependencies.validate_task_plan_execution.assert_called_once_with({'state': 'running'})
        with self.assertRaises(RequiresUserAction) as caught:
            self.commit()
        self.assertEqual(caught.exception.code, 'WRITE_AUTHORIZATION_UNAVAILABLE')
        self.assertEqual(self.effects, ['write'])

    def test_skill_failure_rolls_back_and_skips_plan_guard(self):
        self.install_guards(fail='skill')
        with self.assertRaisesRegex(RuntimeError, 'skill rejected'):
            self.commit()
        self.assertEqual(self.markers(), [])
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'approved')
        self.dependencies.task_plans.guard_authorization_consumption.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_plan_failure_rolls_back_skill_changes_and_authorization(self):
        self.install_guards(fail='plan')
        with self.assertRaises(CapabilityRejected) as caught:
            self.commit()
        self.assertEqual(caught.exception.code, 'PLAN_NOT_ACTIVE')
        self.assertEqual(self.markers(), [])
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'approved')
        self.assertEqual(self.effects, [])

    def test_permission_denial_precedes_authorization_transaction(self):
        self.dependencies.user_grants.require_capability.side_effect = PermissionError('revoked')
        with self.assertRaisesRegex(PermissionError, 'revoked'):
            self.commit()
        self.dependencies.guard_skill_authorization.assert_not_called()
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'approved')
        self.assertEqual(self.effects, [])

    def test_changed_session_rejects_before_downstream(self):
        self.session['last_verified_at'] = '2026-10-04T01:00:00Z'
        handler = Mock()
        with self.assertRaisesRegex(ValueError, 'session changed'):
            self.commit(handler)
        handler.assert_not_called()
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'approved')

    def test_lost_result_after_boundary_keeps_consumption_and_cannot_replay(self):
        def lose_result(_adapter, _worker, _plan, *, enter_commit_boundary):
            enter_commit_boundary()
            self.effects.append('write')
            raise ConnectionError('lost response')
        with self.assertRaises(OutcomeUnknown) as caught:
            self.commit(lose_result)
        self.assertEqual(caught.exception.code, 'RESULT_UNKNOWN')
        self.assertEqual(self.authorizations.get(self.auth_id)['state'], 'consumed')
        with self.assertRaises(RequiresUserAction):
            self.commit(lose_result)
        self.assertEqual(self.effects, ['write'])

    def test_standalone_prepare_freezes_target_and_uses_supplied_interaction(self):
        context = replace(self.context, spec=replace(self.spec, name='test.record.save.prepare'), operation_id='prepare-b')
        prepared = {'plan': {'target': 'record-b'}, 'summary': {'title': 'Offline prepare'}}
        definition = dict(prepare_function='offline_prepare', commit_capability=self.spec.name,
                          context_fields=('target',), authorization_message='Approve test')
        with patch('agentbridge.core.controlled_write_executor.resolve_write_function', return_value=lambda *_: prepared):
            with self.assertRaises(RequiresUserAction) as caught:
                self.executor.prepare(context=context, session=self.session, adapter=object(), worker=object(),
                                      arguments={'target': 'record-b'}, field_submission=None, definition=definition)
        action = caught.exception.next_action
        frozen = self.authorizations.get(action['authorizationId'], include_plan=True)
        self.assertEqual(frozen['plan']['resume_arguments'], {'target': 'record-b'})
        self.assertEqual(frozen['state'], 'pending')
        self.assertEqual(action['interactionId'], 'authorization-test')
        self.assertTrue(action['cardUrl'].startswith('http://127.0.0.1:8780/authorize/'))
        self.dependencies.execution_authorization_interaction.assert_called_once()
        self.dependencies.user_grants.require_capability.assert_not_called()

    def test_service_uses_registry_card_address_and_callback_replaced_before_call(self):
        service = CentralCapabilityService(home=self.db.parent / 'central',
            base_url='https://oa.example.test', registry=self.dependencies.registry)
        replacement = CapabilityRegistry()
        replacement.register(replace(self.spec, version='0.2.0'))
        service.registry = replacement
        service.trusted_card_base_url = 'https://cards.example.test'
        context = replace(self.context, spec=replace(self.spec, name='test.record.save.prepare'))
        definition = dict(prepare_function='offline_prepare', commit_capability=self.spec.name,
                          context_fields=('target',), authorization_message='Approve test')
        with patch.object(service, '_execution_authorization_interaction',
                          return_value={'interactionId': 'replacement'}) as interaction, \
                patch('agentbridge.core.controlled_write_executor.resolve_write_function',
                      return_value=lambda *_: {'plan': {}, 'summary': {}}):
            with self.assertRaises(RequiresUserAction) as caught:
                service._controlled_writes().prepare(context=context, session=self.session,
                    adapter=object(), worker=object(), arguments={'target': 'record-c'},
                    field_submission=None, definition=definition)
        action = caught.exception.next_action
        authorization = service.write_authorizations.get(action['authorizationId'])
        self.assertEqual(authorization['capability_version'], '0.2.0')
        self.assertTrue(action['cardUrl'].startswith('https://cards.example.test/authorize/'))
        self.assertEqual(action['interactionId'], 'replacement')
        interaction.assert_called_once()
