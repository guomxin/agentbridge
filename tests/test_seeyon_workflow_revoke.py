import unittest
from unittest.mock import Mock, patch

from agentbridge.adapters.seeyon_workflow_revoke import (
    WorkflowRevokeContractMismatch,
    WorkflowRevokeOutcomeUnknown,
    _assert_revoked_wait_send_target,
    _wait_for_revoke_readback,
    normalize_workflow_revoke_inputs,
    prepare_workflow_revoke,
    revoke_workflow,
    workflow_revoke_contract_fingerprint,
)


class WorkflowRevokeTests(unittest.TestCase):
    def test_normalize_requires_one_bounded_comment(self):
        self.assertEqual(
            normalize_workflow_revoke_inputs(
                {"affair_id": " affair-1 ", "repeal_comment": " 测试完成，撤销 "}
            ),
            {"affair_id": "affair-1", "repeal_comment": "测试完成，撤销"},
        )
        with self.assertRaises(ValueError):
            normalize_workflow_revoke_inputs(
                {"affair_id": "affair-1", "repeal_comment": ""}
            )
        with self.assertRaises(ValueError):
            normalize_workflow_revoke_inputs(
                {"affair_id": "affair-1", "repeal_comment": "x" * 101}
            )

    def test_prepare_freezes_exact_sent_identity_after_read_only_checks(self):
        row = _sent_row()
        page = object()
        with (
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._resolve_collection_row",
                return_value=(row, page),
            ) as resolve,
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._check_revoke_eligibility"
            ) as precheck,
        ):
            prepared = prepare_workflow_revoke(
                object(),
                object(),
                {"affair_id": "affair-1", "repeal_comment": "自动化测试结束"},
            )

        resolve.assert_called_once_with(
            unittest.mock.ANY,
            unittest.mock.ANY,
            collection="sent",
            affair_id="affair-1",
        )
        precheck.assert_called_once_with(page, row)
        plan = prepared["plan"]
        self.assertEqual(plan["business_intent"], "revoke_sent_workflow")
        self.assertEqual(plan["target"]["summary_id"], "summary-1")
        self.assertEqual(plan["target"]["process_id"], "process-1")
        self.assertEqual(plan["exact_input"]["repeal_comment"], "自动化测试结束")
        self.assertEqual(
            plan["action_contract"]["fingerprint"],
            workflow_revoke_contract_fingerprint(),
        )
        self.assertIn("不可", prepared["summary"]["authorization_notice"])

    def test_commit_consumes_authorization_immediately_before_native_confirm(self):
        row = _sent_row()
        page = object()
        frame = object()
        events = []

        def boundary():
            events.append("boundary")

        with (
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._resolve_collection_row",
                return_value=(row, page),
            ),
            patch("agentbridge.adapters.seeyon_workflow_revoke._check_revoke_eligibility"),
            patch("agentbridge.adapters.seeyon_workflow_revoke._select_exact_sent_row"),
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._open_revoke_dialog",
                return_value=frame,
            ),
            patch("agentbridge.adapters.seeyon_workflow_revoke._fill_revoke_comment"),
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._confirm_revoke_dialog",
                side_effect=lambda _page: events.append("confirm"),
            ),
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._wait_for_revoke_readback",
                side_effect=lambda *_args, **_kwargs: events.append("verify")
                or {"state": 2, "sub_state": 3, "sub_state_name": "撤销"},
            ),
        ):
            result = revoke_workflow(
                object(),
                object(),
                _plan(row),
                enter_commit_boundary=boundary,
            )

        self.assertEqual(events, ["boundary", "confirm", "verify"])
        self.assertTrue(result["workflow_revoked"])
        self.assertTrue(result["verification"]["confirmed"])
        self.assertEqual(result["target"]["affair_id"], "affair-1")

    def test_commit_refuses_changed_target_before_consuming_authorization(self):
        changed = {**_sent_row(), "summary_id": "summary-2"}
        boundary = Mock()
        with patch(
            "agentbridge.adapters.seeyon_workflow_revoke._resolve_collection_row",
            return_value=(changed, object()),
        ):
            with self.assertRaises(WorkflowRevokeContractMismatch):
                revoke_workflow(
                    object(),
                    object(),
                    _plan(_sent_row()),
                    enter_commit_boundary=boundary,
                )
        boundary.assert_not_called()

    def test_failure_after_authorization_consumption_is_unknown_and_not_retryable(self):
        row = _sent_row()
        boundary = Mock()
        with (
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._resolve_collection_row",
                return_value=(row, object()),
            ),
            patch("agentbridge.adapters.seeyon_workflow_revoke._check_revoke_eligibility"),
            patch("agentbridge.adapters.seeyon_workflow_revoke._select_exact_sent_row"),
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._open_revoke_dialog",
                return_value=object(),
            ),
            patch("agentbridge.adapters.seeyon_workflow_revoke._fill_revoke_comment"),
            patch(
                "agentbridge.adapters.seeyon_workflow_revoke._confirm_revoke_dialog",
                side_effect=RuntimeError("transport closed"),
            ),
        ):
            with self.assertRaises(WorkflowRevokeOutcomeUnknown):
                revoke_workflow(
                    object(),
                    object(),
                    _plan(row),
                    enter_commit_boundary=boundary,
                )
        boundary.assert_called_once_with()

    def test_readback_pumps_original_action_page_before_collection_checks(self):
        target = _plan(_sent_row())["target"]
        action_page = Mock()
        revoked = {
            **target,
            "state": 2,
            "sub_state": 3,
            "sub_state_name": "撤销",
        }
        with patch(
            "agentbridge.adapters.seeyon_workflow_revoke._resolve_collection_row",
            side_effect=[(None, object()), (revoked, object())],
        ):
            result = _wait_for_revoke_readback(
                object(),
                object(),
                target=target,
                action_page=action_page,
                timeout_seconds=5,
            )

        action_page.wait_for_timeout.assert_called_once_with(250)
        self.assertEqual(result["sub_state_name"], "撤销")
    def test_wait_send_verification_requires_same_identity_and_revoked_state(self):
        target = _plan(_sent_row())["target"]
        _assert_revoked_wait_send_target(
            target,
            {
                **target,
                "state": 2,
                "sub_state": 3,
                "sub_state_name": "撤销",
            },
        )
        with self.assertRaises(WorkflowRevokeOutcomeUnknown):
            _assert_revoked_wait_send_target(
                target,
                {
                    **target,
                    "summary_id": "other-summary",
                    "state": 2,
                    "sub_state": 3,
                    "sub_state_name": "撤销",
                },
            )
        with self.assertRaises(WorkflowRevokeOutcomeUnknown):
            _assert_revoked_wait_send_target(
                target,
                {**target, "state": 0, "sub_state": 1, "sub_state_name": "草稿"},
            )


def _sent_row() -> dict:
    return {
        "affair_id": "affair-1",
        "summary_id": "summary-1",
        "process_id": "process-1",
        "title": "【HR】请假申请单-辛国茂-事假",
        "create_date": "2026-07-21 11:46",
        "current_nodes": "审批人",
        "body_type": "20",
        "template_id": "template-1",
        "form_app_id": "form-app-1",
        "form_record_id": "form-record-1",
        "flow_finished": False,
        "state": 0,
        "sub_state": 0,
        "sub_state_name": "",
        "summary_state": 0,
        "affair_state": 2,
    }


def _plan(row: dict) -> dict:
    return {
        "schema_version": "agentbridge.oa_workflow_revoke_plan.v1",
        "business_intent": "revoke_sent_workflow",
        "target": {
            key: row[key]
            for key in (
                "affair_id",
                "summary_id",
                "process_id",
                "title",
                "create_date",
                "current_nodes",
                "body_type",
                "template_id",
                "form_app_id",
                "form_record_id",
                "summary_state",
            )
        },
        "action_contract": {
            "version": "seeyon-workflow-revoke-v1",
            "fingerprint": workflow_revoke_contract_fingerprint(),
        },
        "exact_input": {"repeal_comment": "自动化测试结束"},
    }


if __name__ == "__main__":
    unittest.main()
