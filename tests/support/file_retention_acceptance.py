"""Synthetic retained CSV seed shared by isolated Workspace tests and Chrome QA."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentbridge.database.independent import IndependentDatabase
from agentbridge.database.reports import Reports
from agentbridge.database.sources import Sources
from tests.database_fixtures import configured_source


def seed_retained_csv(service, *, user_subject, task_id,
                      source_id="history_reports", entry_seconds=-1):
    if Sources(service.home).get(source_id, required=False) is None:
        configured_source(service.home, source_id, pack="generic")
    runtime = IndependentDatabase(service.home)
    capabilities = ["database.free.read", "database.report.export"]
    if runtime.grants.get(user_subject, source_id)["capabilities"] != capabilities:
        runtime.grants.set(user_subject, capabilities, source_id=source_id)
    reports = Reports(runtime)
    query = {"columns": ["name"], "rows": [{"name": "合成历史报告"}],
             "queried_at": datetime.now(timezone.utc).isoformat(), "sql_sha256": "synthetic-history"}
    with patch.object(runtime, "_execute", return_value=query):
        exported = reports.export(user_subject, source_id, {
            "query_capability": "database.free.read",
            "query_arguments": {"sql": "SELECT name FROM public.devices"},
            "request_key": task_id,
        })
    delivery = reports.download(user_subject, source_id, {"report_id": exported["report_id"]})
    path = f"/api/database/reports/{source_id}/{exported['report_id']}/download"
    artifact, _ = service.tasks.link_artifact(
        task_id=task_id, user_subject=user_subject,
        artifact={"artifact_type": "database_csv", "source_ref": f"{exported['report_id']}:{task_id}",
                  "filename": delivery["filename"], "content_type": "text/csv",
                  "byte_size": delivery["byte_size"], "download_url": path,
                  "expires_at": delivery["download_expires_at"]},
    )
    entry_expiry = (datetime.now(timezone.utc) + timedelta(seconds=entry_seconds)).isoformat()
    payload = reports.payloads.load(exported["report_id"])
    payload["download_expires_at"] = entry_expiry
    reports.payloads.save(exported["report_id"], payload)
    with service.tasks._connect() as db:
        db.execute("UPDATE task_artifacts SET expires_at=? WHERE artifact_id=?", (entry_expiry, artifact["artifact_id"]))
    return {"runtime": runtime, "reports": reports, "artifact_id": artifact["artifact_id"],
            "task_id": task_id, "source_id": source_id, "report_id": exported["report_id"],
            "download_url": path, "entry_expires_at": entry_expiry,
            "report_expires_at": exported["reportExpiresAt"], "delivery": delivery}
