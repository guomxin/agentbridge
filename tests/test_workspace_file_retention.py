"""Retained file metadata and entry renewal use original, authorized report bytes."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

import pytest

from agentbridge.database.independent import DatabaseRejected
from agentbridge.workspace.application import WorkspaceApplication, WorkspaceArtifactError
from tests.test_workspace import _service, _create_account
from tests.support.file_retention_acceptance import seed_retained_csv


@pytest.fixture
def retained_csv(tmp_path):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    task, _ = service.tasks.ensure_task(user_subject=account["user_subject"], agent_host="openclaw",
        host_task_key=str(uuid4()), origin_endpoint_id=account["endpoint_id"],
        active_conversation_ref=account["openclaw_session_key"], title="合成历史CSV")
    sample = seed_retained_csv(service, user_subject=account["user_subject"], task_id=task["task_id"])
    app = WorkspaceApplication(service=service)
    try:
        yield service, account, app, sample
    finally:
        app.close()


def test_report_metadata_and_renewal_preserve_bytes_and_retention(retained_csv):
    service, account, app, sample = retained_csv
    reports, runtime = sample["reports"], sample["runtime"]
    args = (account["user_subject"], sample["source_id"], sample["report_id"])
    original = reports.payloads.load(sample["report_id"])
    metadata = reports.describe(*args)
    assert metadata["state"] == "expired" and metadata["can_reissue"] is True
    assert metadata["report_expires_at"] == sample["report_expires_at"]
    assert metadata["download_url"] is None
    assert "file" not in metadata and "content_base64" not in metadata
    assert reports.payloads.load(sample["report_id"]) == original
    with patch.object(runtime, "_execute", side_effect=AssertionError("must not requery")):
        renewed = reports.renew_web_download(*args)
    after = reports.payloads.load(sample["report_id"])
    assert renewed["state"] == "ready" and renewed["download_url"] == sample["download_url"]
    assert after["content_base64"] == original["content_base64"]
    assert after["sha256"] == original["sha256"]
    assert after["expires_at"] == original["expires_at"]
    assert renewed["download_expires_at"] <= renewed["report_expires_at"]


def test_report_metadata_and_renewal_reject_foreign_source_revoked_or_expired(retained_csv):
    service, account, app, sample = retained_csv
    reports, runtime = sample["reports"], sample["runtime"]
    for owner, source in [("user-b", sample["source_id"]), (account["user_subject"], "other_source")]:
        for method in [reports.describe, reports.renew_web_download]:
            with pytest.raises(DatabaseRejected):
                method(owner, source, sample["report_id"])
    runtime.grants.set(account["user_subject"], [], source_id=sample["source_id"])
    for method in [reports.describe, reports.renew_web_download]:
        with pytest.raises(DatabaseRejected):
            method(account["user_subject"], sample["source_id"], sample["report_id"])
    runtime.grants.set(account["user_subject"], ["database.free.read", "database.report.export"], source_id=sample["source_id"])
    # A restored grant revision does not restore the original frozen access.
    with pytest.raises(DatabaseRejected, match="AUTHORIZATION_CHANGED"):
        reports.describe(account["user_subject"], sample["source_id"], sample["report_id"])


def test_retained_report_cannot_be_renewed_after_report_expiry(retained_csv):
    service, account, app, sample = retained_csv
    reports = sample["reports"]
    old = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    import sqlite3
    with sqlite3.connect(reports.path) as db:
        db.execute("UPDATE reports SET expires=? WHERE id=?", (old, sample["report_id"]))
    for method in [reports.describe, reports.renew_web_download]:
        with pytest.raises(DatabaseRejected, match="REPORT_UNAVAILABLE"):
            method(account["user_subject"], sample["source_id"], sample["report_id"])
    artifact = app.task_detail(account, sample["task_id"])["artifacts"][0]
    assert artifact["can_reissue"] is False and not artifact.get("report_expires_at")
    with pytest.raises(WorkspaceArtifactError):
        app.reissue_artifact(account, task_id=sample["task_id"], artifact_id=sample["artifact_id"])


def test_workspace_csv_entry_renewal_uses_original_report(retained_csv):
    service, account, app, sample = retained_csv
    artifact = next(item for item in app.task_detail(account, sample["task_id"])["artifacts"] if item["artifact_id"] == sample["artifact_id"])
    assert artifact["state"] == "expired" and artifact["can_reissue"] is True
    assert artifact["report_expires_at"] == sample["report_expires_at"]
    assert artifact["download_url"] is None
    with patch("agentbridge.database.independent.IndependentDatabase._execute", side_effect=AssertionError("must not requery")):
        detail = app.reissue_artifact(account, task_id=sample["task_id"], artifact_id=sample["artifact_id"])
    renewed = next(item for item in detail["artifacts"] if item["artifact_id"] == sample["artifact_id"])
    assert renewed["state"] == "ready" and renewed["download_url"] == sample["download_url"]


def test_workspace_revoked_report_has_no_download_or_renewal_metadata(retained_csv):
    service, account, app, sample = retained_csv
    sample["runtime"].grants.set(account["user_subject"], [], source_id=sample["source_id"])
    detail = app.task_detail(account, sample["task_id"])
    artifact = next(item for item in detail["artifacts"] if item["artifact_id"] == sample["artifact_id"])
    assert artifact["can_reissue"] is False
    assert not artifact.get("download_url") and not artifact.get("report_expires_at")
    with pytest.raises((WorkspaceArtifactError, PermissionError)):
        app.reissue_artifact(account, task_id=sample["task_id"], artifact_id=sample["artifact_id"])


def test_workspace_csv_renewal_rejects_foreign_task_and_changed_reference(retained_csv):
    service, account, app, sample = retained_csv
    other = _create_account(service, user_subject="user-b", username="bob", endpoint_key="telegram:*:bob")
    with pytest.raises((KeyError, PermissionError)):
        app.reissue_artifact(other, task_id=sample["task_id"], artifact_id=sample["artifact_id"])
    before = sample["reports"].payloads.load(sample["report_id"])
    with service.tasks._connect() as db:
        db.execute("UPDATE task_artifacts SET download_url=? WHERE artifact_id=?",
            (sample["download_url"].replace(sample["report_id"], "f" * 32), sample["artifact_id"]))
    with pytest.raises(WorkspaceArtifactError):
        app.reissue_artifact(account, task_id=sample["task_id"], artifact_id=sample["artifact_id"])
    assert sample["reports"].payloads.load(sample["report_id"]) == before


def test_file_history_matches_detail_after_renewal_and_revocation(retained_csv):
    service, account, app, sample = retained_csv
    app.reissue_artifact(account, task_id=sample["task_id"], artifact_id=sample["artifact_id"])
    def history_artifact():
        return next(item for group in app.artifact_history(account) for item in group["artifacts"]
                    if item["artifact_id"] == sample["artifact_id"])
    assert history_artifact()["state"] == "ready"
    assert history_artifact()["can_reissue"] is True
    sample["runtime"].grants.set(account["user_subject"], [], source_id=sample["source_id"])
    assert history_artifact()["download_url"] is None
    assert history_artifact()["can_reissue"] is False


@pytest.mark.parametrize("attachments", [None, 123, "old-invalid-shape", {"attachmentId":"untrusted"}])
def test_old_malformed_attachment_payload_does_not_break_history(retained_csv, attachments):
    service, account, app, sample = retained_csv
    service.tasks.append_timeline_message(user_subject=account["user_subject"],
        source_endpoint_id=account["endpoint_id"], message_key="malformed-legacy-media",
        role="assistant", text="旧消息文字保留", payload={"attachments":[]})
    import json
    with service.tasks._connect() as db:
        db.execute("UPDATE user_timeline SET payload_json=? WHERE user_subject=? AND dedupe_key=?",
            (json.dumps({"attachments":attachments}), account["user_subject"], "message:malformed-legacy-media"))
    assert any(row["text"] == "旧消息文字保留" for row in app.current(account)["timeline"]["items"])


def test_file_history_rechecks_task_access(retained_csv):
    service, account, app, sample = retained_csv
    with patch.object(service, "require_task_result_access", side_effect=PermissionError("revoked")):
        assert app.artifact_history(account) == []
