"""Retained text is private data, never a replacement execution capability."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agentbridge.auth.business_draft_card import BusinessDraftApplication, business_draft_target
from agentbridge.core.business_draft_profiles import PROFILES
from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.business_content_drafts import MAX_CONTENTS_PER_SUBJECT
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
from tests.authorization_fixtures import grant_permissions
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, OWNER


class BusinessContentDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = FullBusinessAcceptanceFixture(Path(self.temp.name), seed=False, assign_skill=False)
        self.addCleanup(self.fixture.close)
        self.service = self.fixture.service
        self.drafts = BusinessDraftService(self.service)
        self.case = self.fixture.add_pending_case("leave")
        self.args, self.state = self.open(self.case["input_id"])

    def open(self, source_id):
        opened = self.drafts.open(source_id)
        return ({"source_submission_id": source_id, "csrf_token": opened["csrfToken"],
                 "csrf_cookie": opened["csrfToken"]}, opened["state"])

    def sql(self, query, values=()):
        with sqlite3.connect(self.service.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def new_source(self, *, case=None, same_task=False):
        case = case or self.case
        # A real expired field ticket releases the existing prepare collision;
        # the old task and decisions remain unchanged. No guard is bypassed.
        old = self.service.field_submissions.get(case["input_id"])
        expiry = datetime.fromisoformat(old["expires_at"]) + timedelta(seconds=1)
        with patch.object(self.service.field_submissions, "clock", return_value=expiry):
            self.service.field_submissions.get(case["input_id"])
        task = case["task_id"] if same_task else self.service.ensure_host_task(user_subject=OWNER,
            token_id="fixture-token", agent_host="openclaw", host_task_key="content-test:" + uuid4().hex,
            endpoint_key=self.fixture.account["endpoint_key"], client_type="web",
            external_subject=self.fixture.account["account_id"],
            conversation_ref=self.fixture.account["openclaw_session_key"], title="合成内容恢复") ["task"]["taskId"]
        result = self.service.invoke(user_subject=OWNER, capability_name=old["capability_name"],
            arguments=case.get("arguments", {}), task_id=task, idempotency_key="content-source:" + uuid4().hex)
        self.assertEqual(result["status"], "requires_user_action", result)
        return self.open(result["nextAction"]["inputSubmissionId"])

    def save(self, *, args=None, state=None, values=None):
        return self.drafts.save(**(args or self.args), expected_revision=(state or self.state)["revision"],
            values=values or {"opinion": "尚未写完的私人意见"})

    def restore_args(self, args, state):
        candidate = state["contentDraft"]["restore"]
        self.assertIsNotNone(candidate, state["contentDraft"])
        return {**args, "expected_revision": state["revision"], "content_id": candidate["contentId"],
            "content_version": candidate["version"], "request_key": "restore-content-" + uuid4().hex}

    def test_ciphertext_only_and_get_is_metadata_without_reads_or_copy(self):
        saved = self.save()
        row = self.sql("SELECT * FROM private_business_draft_contents")[0]
        self.assertNotIn("尚未写完".encode(), row["ciphertext"])
        self.assertNotIn("尚未写完", json.dumps({k:v for k,v in row.items() if k != "ciphertext"}))
        self.assertEqual(datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["saved_at"]), timedelta(hours=24))
        args, state = self.new_source()
        before = len(self.fixture.oa.data["calls"])
        reread = self.drafts.state(**args)
        self.assertEqual(len(self.fixture.oa.data["calls"]), before)
        self.assertEqual(reread["values"], {"opinion": ""})
        self.assertEqual(reread["contentDraft"]["restore"]["scope"], "same_item")
        self.assertNotIn("尚未写完", repr(reread))
        self.assertEqual(len(self.sql("SELECT * FROM write_authorizations")), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_restore_partial_values_reads_materials_clears_preview_and_replays_once(self):
        saved = self.save(values={"opinion": ""})
        expiry = saved["contentDraft"]["saved"]["expiresAt"]
        args, state = self.new_source()
        request = self.restore_args(args, state)
        before = len(self.fixture.oa.data["calls"])
        restored = self.drafts.restore_content(**request)
        self.assertGreater(len(self.fixture.oa.data["calls"]), before)
        self.assertEqual(restored["revision"], state["revision"] + 1)
        self.assertEqual(restored["values"], {"opinion": ""})
        self.assertIsNone(restored["preview"])
        self.assertIsNone(restored["receipt"])
        self.assertNotIn("approve", restored["allowedActions"])
        self.assertEqual(restored["contentDraft"]["saved"]["expiresAt"], expiry)
        before = len(self.fixture.oa.data["calls"])
        replay = self.drafts.restore_content(**request)
        self.assertEqual(replay["revision"], restored["revision"])
        self.assertEqual(len(self.fixture.oa.data["calls"]), before)
        self.assertEqual(len(self.sql("SELECT * FROM task_events WHERE event_type='task.business_content.changed'")), 1)
        self.assertEqual(len(self.sql("SELECT * FROM write_authorizations")), 0)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_encrypted_row_transplant_or_metadata_tamper_never_restores(self):
        self.save()
        args, state = self.new_source()
        request = self.restore_args(args, state)
        other = self.fixture.add_pending_case("leave")
        other_args, other_state = self.open(other["input_id"])
        self.save(args=other_args, state=other_state, values={"opinion": "另一个事项的私人文字"})
        cipher = self.sql("SELECT ciphertext FROM private_business_draft_contents WHERE source_submission_id=?", (other["input_id"],))[0]["ciphertext"]
        self.sql("UPDATE private_business_draft_contents SET ciphertext=? WHERE content_id=?", (cipher, request["content_id"]))
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_CONTENT_UNAVAILABLE")
        self.assertEqual(self.drafts.state(**args)["values"], {"opinion": ""})

    def test_saved_version_and_current_revision_prevent_concurrent_overwrite(self):
        saved = self.save()
        args, state = self.new_source()
        request = self.restore_args(args, state)
        # A new edit to the source changes both its content version and AAD.
        self.sql("UPDATE private_business_draft_contents SET version=version+1,source_revision=source_revision+1 WHERE content_id=?", (request["content_id"],))
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_CONTENT_CHANGED")
        state = self.drafts.state(**args)
        request = self.restore_args(args, state)
        self.save(args=args, state=state, values={"opinion": "新页自己的内容"})
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_REVISION_CONFLICT")
        self.assertEqual(self.drafts.state(**args)["values"], {"opinion": "新页自己的内容"})

    def test_mid_read_edit_and_live_identity_mismatch_preserve_current_values(self):
        self.save()
        args, state = self.new_source()
        request = self.restore_args(args, state)
        original = self.drafts._read_downstream
        def changed(*a, **kw):
            result = original(*a, **kw)
            self.save(args=args, state=state, values={"opinion": "读取期间的编辑"})
            return result
        with patch.object(self.drafts, "_read_downstream", side_effect=changed):
            with self.assertRaises(BusinessDraftError):
                self.drafts.restore_content(**request)
        self.assertEqual(self.drafts.state(**args)["values"], {"opinion": "读取期间的编辑"})
        state = self.drafts.state(**args)
        request = self.restore_args(args, state)
        adapter = self.service._adapters_by_system["oa"]
        with patch.object(adapter, "probe_business_draft_session", return_value={"observed_principal_ref":"other"}):
            with self.assertRaises(BusinessDraftError) as caught:
                self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_SESSION_CHANGED")

    def test_account_rebind_and_matching_title_different_instance_are_ineligible(self):
        self.save()
        other = self.fixture.add_pending_case("leave")
        _, state = self.open(other["input_id"])
        self.assertIsNone(state["contentDraft"]["restore"])
        self.sql("UPDATE sessions SET expected_principal_ref='other',downstream_principal_ref='other' WHERE system_id='oa'")
        state = self.drafts.state(**self.args)
        self.assertIsNone(state["contentDraft"]["saved"])
        self.assertEqual(state["contentDraft"]["reason"], "DRAFT_CONTENT_IDENTITY_UNAVAILABLE")
        with self.assertRaises(BusinessDraftError):
            self.save(state=state)

    def test_revoked_grant_and_changed_live_target_block_restore_before_copy(self):
        self.save()
        args, state = self.new_source()
        request = self.restore_args(args, state)
        target = self.fixture.oa.data["affairs"][self.case["arguments"]["affair_id"]]
        target["signals"]["identity"]["process_id"] = "another-process"
        self.fixture.oa.persist()
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_CONTENT_TARGET_CHANGED")
        self.assertEqual(self.drafts.state(**args)["values"], {"opinion":""})
        grant_permissions(self.service.user_grants, OWNER, [])
        before = len(self.fixture.oa.data["calls"])
        with self.assertRaises(BusinessDraftError):
            self.drafts.restore_content(**request)
        self.assertEqual(len(self.fixture.oa.data["calls"]), before)

    def test_legacy_first_open_after_rebind_never_attests_current_account(self):
        # Produce an actual immutable old-style source, then open it for the
        # first time after the persisted account has changed.
        with patch("agentbridge.core.business_content_identity.principal_binding", return_value=None):
            case = self.fixture.add_case()
        self.sql("UPDATE sessions SET expected_principal_ref='other',downstream_principal_ref='other' WHERE system_id='taihua'")
        args, state = self.open(case["input_id"])
        values = {key:"" for key in state["values"]}
        values["content"] = "旧源仍可保持短期编辑"
        saved = self.save(args=args, state=state, values=values)
        self.assertEqual(saved["values"], values)
        self.assertIsNone(saved["contentDraft"]["saved"])
        self.assertEqual(saved["contentDraft"]["reason"], "DRAFT_CONTENT_LEGACY_SOURCE")
        self.assertEqual(self.sql("SELECT * FROM private_business_draft_contents"), [])

    def test_legacy_source_without_retention_still_saves_previews_and_finalizes(self):
        with patch("agentbridge.core.business_content_identity.principal_binding", return_value=None):
            case = self.fixture.add_case()
        args, state = self.open(case["input_id"])
        saved = self.save(args=args, state=state, values={"log_date":"2026-10-23", "hours":"2", "project":"SYNTHETIC", "content":"旧版表单正常完成"})
        ready = self.drafts.preview(**args, expected_revision=saved["revision"])
        receipt = self.drafts.approve(**args, expected_revision=ready["revision"], preview_id=ready["preview"]["previewId"],
            request_key="legacy-content-no-retention")
        self.assertIsNotNone(receipt["receipt"])
        self.assertEqual(self.sql("SELECT * FROM private_business_draft_contents"), [])

    def test_creation_has_no_instance_key_and_only_restores_in_same_task(self):
        case = self.fixture.add_case()
        args, state = self.open(case["input_id"])
        values = {key:"" for key in state["values"]}
        values["content"] = "创建类半填内容"
        self.save(args=args, state=state, values=values)
        _, other = self.new_source(case=case)
        self.assertIsNone(other["contentDraft"]["restore"])
        same_args, same = self.new_source(case=case, same_task=True)
        self.assertEqual(same["contentDraft"]["restore"]["scope"], "same_task")
        result = self.drafts.restore_content(**self.restore_args(same_args, same))
        self.assertEqual(result["values"], values)
        self.assertEqual(self.fixture.transport.write_calls, 0)

    def test_room_restore_clears_old_choice_persistently_before_requery(self):
        case = self.fixture.add_view_case("meeting_room")
        args, state = self.open(case["input_id"])
        from tests.support.business_view_acceptance import ROOM_VALUES
        saved = self.save(args=args, state=state, values=ROOM_VALUES)
        saved = self.drafts.options(**args, expected_revision=saved["revision"])
        saved = self.save(args=args, state=saved, values={**saved["values"], "room":saved["options"]["items"][0]["value"]})
        newer_args, newer = self.new_source(case=case, same_task=True)
        restored = self.drafts.restore_content(**self.restore_args(newer_args, newer))
        self.assertEqual(restored["values"]["room"], "")
        self.assertIsNone(restored["options"])
        self.assertEqual(self.drafts.state(**newer_args)["values"]["room"], "")
        self.assertIsNone(self.drafts.state(**newer_args)["preview"])

    def test_legacy_field_submission_cannot_leave_restorable_approved_text(self):
        saved = self.save()
        self.service.field_submissions.submit(self.case["input_id"], csrf_token=self.args["csrf_token"],
            csrf_cookie=self.args["csrf_cookie"], values=saved["values"])
        self.drafts.store.cleanup()
        self.assertEqual(self.sql("SELECT * FROM private_business_draft_contents"), [])

    def test_retention_quota_evicts_oldest_and_crypto_failure_rolls_back_save(self):
        source = self.service.field_submissions.get(self.case["input_id"])
        now = self.drafts.store.clock()
        with self.drafts.store._connect() as connection:
            draft = self.drafts._draft(connection, source["submission_id"])
            for index in range(MAX_CONTENTS_PER_SUBJECT + 2):
                candidate = {**source, "submission_id":f"storage-contract-{index:032d}"}
                self.drafts.contents.save(connection, candidate, draft, {"opinion":"半填"}, now=now + timedelta(seconds=index))
        rows = self.sql("SELECT source_submission_id FROM private_business_draft_contents ORDER BY updated_at")
        self.assertEqual(len(rows), MAX_CONTENTS_PER_SUBJECT)
        self.assertTrue(rows[0]["source_submission_id"].endswith("02"))
        with patch.object(self.service.session_states.protector, "protect", side_effect=RuntimeError("synthetic crypto failure")):
            with self.assertRaises(BusinessDraftError) as caught:
                self.save()
        self.assertEqual(caught.exception.code, "DRAFT_CONTENT_SAVE_UNAVAILABLE")
        self.assertEqual(self.drafts.state(**self.args)["revision"], 1)
        self.assertEqual(self.drafts.state(**self.args)["values"], {"opinion":""})

    def test_source_expiry_retains_text_but_24h_expiry_deletes_it(self):
        saved = self.save()
        source = self.service.field_submissions.get(self.case["input_id"])
        source_expiry = datetime.fromisoformat(source["expires_at"])
        self.service.business_drafts.clock = lambda: source_expiry + timedelta(seconds=1)
        self.service.business_drafts.cleanup()
        self.assertEqual(self.sql("SELECT values_json,state FROM business_drafts WHERE source_submission_id=?", (self.case["input_id"],))[0], {"values_json":"{}", "state":"expired"})
        self.assertEqual(len(self.sql("SELECT * FROM private_business_draft_contents")), 1)
        self.service.business_drafts.clock = lambda: datetime.fromisoformat(saved["contentDraft"]["saved"]["expiresAt"])
        self.service.business_drafts.cleanup()
        self.assertEqual(self.sql("SELECT * FROM private_business_draft_contents"), [])

    def test_discard_is_idempotent_keeps_current_fields_and_same_key_cannot_change_action(self):
        saved = self.save()
        current = saved["contentDraft"]["saved"]
        request = {**self.args, "expected_revision": saved["revision"], "content_id": current["contentId"],
            "content_version": current["version"], "request_key": "discard-content-request"}
        result = self.drafts.discard_content(**request)
        self.assertEqual(result["values"], saved["values"])
        self.assertIsNone(result["contentDraft"]["saved"])
        self.assertEqual(self.drafts.discard_content(**request), result)
        with self.assertRaises(BusinessDraftError) as caught:
            self.drafts.restore_content(**request)
        self.assertEqual(caught.exception.code, "DRAFT_CONTENT_REQUEST_CONFLICT")

    def test_discard_same_item_removes_previous_versions_without_erasing_other_item(self):
        self.save()
        args, state = self.new_source()
        restored = self.drafts.restore_content(**self.restore_args(args, state))
        other = self.fixture.add_pending_case("leave")
        other_args, other_state = self.open(other["input_id"])
        self.save(args=other_args, state=other_state, values={"opinion":"别的事项保留内容"})
        current = restored["contentDraft"]["saved"]
        deleted = self.drafts.discard_content(**args, expected_revision=restored["revision"],
            content_id=current["contentId"], content_version=current["version"], request_key="delete-exact-item-content")
        self.assertIsNone(deleted["contentDraft"]["saved"])
        self.assertIsNone(deleted["contentDraft"]["restore"])
        self.assertEqual(deleted["values"], restored["values"])
        remaining = self.sql("SELECT source_submission_id FROM private_business_draft_contents")
        self.assertEqual(remaining, [{"source_submission_id":other["input_id"]}])

    def test_final_decision_removes_matching_content_atomically_and_cannot_inherit_unknown(self):
        saved = self.save()
        ready = self.drafts.preview(**self.args, expected_revision=saved["revision"])
        original = self.drafts.contents.delete_finalized
        def failing(*args):
            original(*args)
            raise RuntimeError("synthetic transaction failure")
        request = {**self.args, "expected_revision": ready["revision"], "preview_id": ready["preview"]["previewId"],
                   "request_key": "content-final-decision"}
        with patch.object(self.drafts.contents, "delete_finalized", side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.drafts.approve(**request)
        self.assertEqual(len(self.sql("SELECT * FROM private_business_draft_contents")), 1)
        self.assertEqual(self.sql("SELECT * FROM business_draft_decisions"), [])
        self.assertEqual(self.sql("SELECT * FROM write_authorizations"), [])
        current = self.drafts.state(**self.args)
        result = self.drafts.approve(**{**request, "preview_id": current["preview"]["previewId"]})
        self.assertIsNotNone(result["receipt"])
        self.assertEqual(self.sql("SELECT * FROM private_business_draft_contents"), [])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_all_registered_field_sets_encrypt_without_requiring_complete_input(self):
        # Storage contract matrix: no preview/commit dependency and no implicit
        # conversion of empty/false-looking strings into execution arguments.
        source = self.service.field_submissions.get(self.case["input_id"])
        with self.drafts.store._connect() as connection:
            draft = self.drafts._draft(connection, self.case["input_id"])
            for profile in PROFILES:
                with self.subTest(profile=profile.profile_id):
                    schema = deepcopy(_TRUSTED_WRITE_DEFINITIONS[profile.prepare]["field_schema"])
                    schema.update({"_agentbridge_business_draft":"v1", "_agentbridge_business_draft_profile":profile.marker,
                        "_agentbridge_content_principal":source["form_schema"]["_agentbridge_content_principal"]})
                    candidate = {**source, "capability_name":profile.prepare, "form_schema":schema}
                    values = {field: "" if index % 2 else "false" for index, field in enumerate(profile.fields)}
                    self.drafts.contents.save(connection, candidate, draft, values, now=self.drafts.store.clock())
                    row = connection.execute("SELECT * FROM private_business_draft_contents WHERE source_submission_id=?", (source["submission_id"],)).fetchone()
                    self.assertEqual(self.drafts.contents.values(candidate, row), values)
                    self.assertEqual(row["profile_id"], profile.profile_id)

    def test_static_enum_contract_changes_isolate_content_but_room_availability_does_not(self):
        from agentbridge.core.business_content_drafts import schema_hash
        original = self.service.field_submissions.get(self.case["input_id"])
        for prepare, field_name, equal in (("oa.leave.submit.prepare", "leave_type", False),
                                         ("oa.meeting_room.application.prepare", "room", True)):
            profile = next(profile for profile in PROFILES if profile.prepare == prepare)
            schema = deepcopy(_TRUSTED_WRITE_DEFINITIONS[prepare]["field_schema"])
            schema.update({"_agentbridge_business_draft":"v1", "_agentbridge_business_draft_profile":profile.marker})
            source = {**original, "capability_name":prepare, "form_schema":schema}
            before = schema_hash(source)
            field = next(field for field in schema["fields"] if field["name"] == field_name)
            field["options"] = [{"value":"different", "label":"changed"}]
            self.assertEqual(before == schema_hash(source), equal)

    def test_content_endpoints_are_closed_and_reject_csrf_or_extra_execution_fields(self):
        saved = self.save()
        args, state = self.new_source()
        candidate = state["contentDraft"]["restore"]
        path = "/flow/input/" + args["source_submission_id"] + "/draft/content-restore"
        self.assertEqual(business_draft_target(path)[2], "content-restore")
        self.assertIsNone(business_draft_target(path + "?result=" + "x" * 43))
        app = BusinessDraftApplication(service=self.service, draft_service=self.drafts)
        payload = {"expectedRevision":state["revision"], "contentId":candidate["contentId"],
                   "contentVersion":candidate["version"], "requestKey":"content-http-request"}
        def call(body, token):
            return app.mutate(args["source_submission_id"], presentation_id=None, action="content-restore", token=token,
                csrf_cookie=args["csrf_cookie"], content_type="application/json", body=json.dumps(body).encode())
        self.assertEqual(call(payload, "wrong").status, 403)
        self.assertEqual(call({**payload, "authorizationId":"forbidden"}, args["csrf_token"]).status, 400)
        self.assertEqual(call({**payload, "contentVersion":True}, args["csrf_token"]).status, 400)
        result = call(payload, args["csrf_token"])
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual(json.loads(result.body)["values"], saved["values"])

    def test_real_unknown_recovery_starts_blank_and_child_retention_is_task_scoped(self):
        from tests.support.telegram_recovery_acceptance import TelegramRecoveryAcceptanceFixture
        from tests.test_telegram_recovery_acceptance import workspace_prepare
        with TemporaryDirectory() as folder:
            fixture = TelegramRecoveryAcceptanceFixture(Path(folder), cases=("workspace-origin",))
            try:
                original = fixture.meta["telegram_recovery_cases"][0]
                before = fixture.audit()
                with fixture.service.tasks._connect() as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM private_business_draft_contents").fetchone()[0], 0)
                child = workspace_prepare(fixture, original)
                task = fixture.service.tasks.get_task(child, user_subject=OWNER)
                interaction = fixture.service.interactions.get(task["current_interaction_id"], user_subject=OWNER)
                drafts = BusinessDraftService(fixture.service)
                opened = drafts.open(interaction["resource_id"])
                self.assertEqual(opened["state"]["values"], {"opinion":""})
                self.assertIsNone(opened["state"]["contentDraft"]["restore"])
                args = {"source_submission_id":interaction["resource_id"], "csrf_token":opened["csrfToken"], "csrf_cookie":opened["csrfToken"]}
                drafts.save(**args, expected_revision=opened["state"]["revision"], values={"opinion":"本恢复任务新写的半填意见"})
                with fixture.service.tasks._connect() as connection:
                    row = connection.execute("SELECT scope_kind,binding_json FROM private_business_draft_contents").fetchone()
                    self.assertEqual(row["scope_kind"], "task")
                    self.assertEqual(json.loads(row["binding_json"])["taskId"], child)
                    self.assertIs(json.loads(row["binding_json"])["recovery"], True)
                after = fixture.audit()
                self.assertEqual(before["decisions"], after["decisions"])
                self.assertEqual(before["syntheticWrites"], after["syntheticWrites"])
                self.assertTrue(after["originals"][0]["authorizationUnchanged"])
            finally:
                fixture.close()

