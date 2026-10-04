"""Pure task decisions checked against pre-refactor method expressions."""
from dataclasses import asdict
import json
from pathlib import Path
import unittest

from bscli.core import task_state_rules as rules
from bscli.core import task_projections as projections
from bscli.core import tasks


CONTRACT = json.loads((Path(__file__).parent / 'fixtures/task_state_contract.json').read_text())


class TaskStateRuleTests(unittest.TestCase):
    def test_operation_observations_batch_precedence_and_unknown_match_baseline(self):
        for status, operation, batch, new, expected in CONTRACT['operation']:
            with self.subTest(task=status, operation=operation, batch=batch, new=new):
                self.assertEqual(asdict(rules.operation_observation(
                    task_status=status, operation_status=operation,
                    batch_state=batch, newly_linked=new,
                )), expected)

    def test_interaction_order_duplicates_terminal_and_login_match_baseline(self):
        for status, state, kind, has_operation, new, current, previous, expected in CONTRACT['interaction']:
            with self.subTest(task=status, state=state, kind=kind, new=new, current=current, previous=previous):
                task = {'status': status, 'current_operation_id': 'operation' if has_operation else None,
                        'current_interaction_id': 'card' if current else 'other'}
                self.assertEqual(asdict(rules.interaction_observation(
                    task=task, interaction_id='card', state=state,
                    interaction_type=kind, newly_linked=new, previous_state=previous,
                )), expected)

    def test_plan_events_and_child_operation_do_not_reopen_terminal_tasks(self):
        for status, event, expected in CONTRACT['plan']:
            with self.subTest(status=status, event=event):
                self.assertEqual(rules.plan_task_status(status, event), expected)

    def test_explicit_terminal_commands_preserve_reuse_and_rejection(self):
        for status, target, expected in CONTRACT['terminal']:
            with self.subTest(status=status, target=target):
                self.assertEqual(rules.terminal_transition(status, target), expected)

    def test_batch_failure_precedence_and_summary_status_match_baseline(self):
        for state, succeeded, expected in CONTRACT['batchFailure']:
            with self.subTest(state=state, succeeded=succeeded):
                self.assertEqual(rules.batch_failure_status(state, succeeded), expected)
        for state, expected in CONTRACT['batchStatus']:
            with self.subTest(batch=state):
                self.assertEqual(rules.batch_task_status(state), expected)

    def test_finished_timestamp_and_unsupported_state_contract(self):
        for state, expected in CONTRACT['finishedAt']:
            self.assertEqual(rules.task_finished_at(state, 'NOW'), expected)
        with self.assertRaisesRegex(ValueError, 'unsupported task status: future'):
            rules.task_finished_at('future', 'NOW')

    def test_legacy_imports_refer_to_shared_rules_and_projections(self):
        for name in ('TASK_STATUSES', 'ACTIVE_TASK_STATUSES', 'TERMINAL_TASK_STATUSES',
                     '_task_status_for_operation', '_event_type_for_operation',
                     '_task_status_for_interaction', '_event_type_for_interaction', '_interaction_may_update_task'):
            self.assertIs(getattr(tasks, name), getattr(rules, name))
        for name in ('_task_from_row', '_batch_item_from_row', '_artifact_from_row',
                     '_continuation_from_row', '_artifact_delivery_aggregate'):
            self.assertIs(getattr(tasks, name), getattr(projections, name))


class TaskProjectionTests(unittest.TestCase):
    def test_frozen_projection_values_errors_and_visibility(self):
        for case in CONTRACT['projections']:
            with self.subTest(projection=case['function'], label=case['label']):
                before = json.loads(json.dumps(case['arguments']))
                try:
                    result = {'result': getattr(projections, case['function'])(**case['arguments'])}
                except Exception as error:
                    result = {'error': type(error).__name__, 'message': str(error)}
                self.assertEqual(result, case['expected'])
                self.assertEqual(case['arguments'], before)


if __name__ == '__main__':
    unittest.main()
