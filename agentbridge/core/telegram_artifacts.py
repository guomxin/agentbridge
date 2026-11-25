"""Deliver an existing task file only to the NanoBot private origin that requested it."""

import base64
import re

from agentbridge.core.endpoint_instances import endpoint_instances
from agentbridge.core.document_downloads import (
    DocumentDownloadAccessDenied,
    DocumentDownloadIntegrityError,
    DocumentDownloadStateError,
)
from agentbridge.core.report_exports import ADDRESSBOOK_REPORT_DOCUMENT_TYPE, SMARTLIGHT_REPORT_DOCUMENT_TYPE
from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_pair_proof, verified_pair


DOCUMENT_TYPES = {
    "certificate_scan": {"patent_certificate", "software_copyright_certificate"},
    "oa_addressbook_report": {ADDRESSBOOK_REPORT_DOCUMENT_TYPE},
    "smartlight_report": {SMARTLIGHT_REPORT_DOCUMENT_TYPE},
}
ARTIFACT_TYPES = {*DOCUMENT_TYPES, "database_csv"}


def origin_pair_proof(db, subject, endpoint, ownership):
    origin = ownership.get(endpoint["endpoint_id"])
    if not origin or origin[0] != "nanobot" or endpoint["client_type"] != "telegram":
        return None
    binding = mirror_binding(db, subject, origin[1], ownership)
    sender = endpoint["external_subject"]
    if not binding or not sender or endpoint["account_id"] != binding["bot"]:
        return None
    bot = binding["bot"]
    if endpoint["endpoint_key"] != f"telegram:{bot}:{sender}" or not verified_pair(
        db, subject, origin[1], bot, sender, allow_new_pair=not binding["all_subjects"]
    ):
        return None
    return mirror_pair_proof(db, subject, binding, sender)


def require_certificate_selection(
    service, subject, task_id, endpoint_id, conversation, instance, download_ids
):
    """Accept only results from certificate searches actually observed by this private task."""
    task = service.tasks.get_task(task_id, user_subject=subject)
    if (
        task["agent_host"] != "nanobot"
        or task["origin_endpoint_id"] != endpoint_id
        or task["active_conversation_ref"] != conversation
    ):
        raise PermissionError("Certificate selection requires its original task and conversation")
    endpoint = service.tasks.get_endpoint(endpoint_id, user_subject=subject)
    with service.tasks._connect() as db:
        ownership = endpoint_instances(db, subject)
        if ownership.get(endpoint_id) != ("nanobot", instance) or not origin_pair_proof(
            db, subject, endpoint, ownership
        ):
            raise PermissionError("The original Telegram pairing is no longer available")
    service.user_grants.require(subject, "oa.certificate.read")
    service.user_grants.require(subject, "oa.certificate.download")
    wanted = set(download_ids)
    for operation_id in service.tasks.operation_ids_for_task(task_id=task_id, user_subject=subject):
        operation = service.operations.get(operation_id)
        if (
            operation["user_subject"] != subject
            or operation["status"] != "succeeded"
            or operation["capability_name"] != "oa.document.certificate.search"
        ):
            continue
        for item in (operation.get("result") or {}).get("items") or []:
            if isinstance(item, dict) and item.get("document_type") in DOCUMENT_TYPES["certificate_scan"]:
                wanted.discard(item.get("download_id"))
        if not wanted:
            return
    raise PermissionError("Certificate not selected from this original task")


def _database_csv(service, subject, task_id, artifact, with_body):
    from agentbridge.database.independent import IndependentDatabase
    from agentbridge.database.reports import Reports

    route = re.fullmatch(
        r"/api/database/reports/([a-z][a-z0-9_-]{0,63})/([0-9a-f]{32})/download", artifact["download_url"]
    )
    if not route or artifact["source_ref"] != f"{route[2]}:{task_id}":
        raise PermissionError("Report and original task do not match")
    source_id, report_id = route.groups()

    def check_query(capability):
        for required in ("database.report.download", capability):
            service.validate_skill_task(subject, task_id, capability=required, source_id=source_id)

    reports = Reports(IndependentDatabase(service.home), check_query=check_query)
    if with_body:
        return reports.web_download(subject, source_id, report_id)
    if reports.describe(subject, source_id, report_id)["state"] != "ready":
        raise PermissionError("Report download has expired")
    return {}


def authorized_artifact(service, delivery, endpoint, instance, *, with_body):
    """Read retained bytes by owner and logical ID. Never fetch or forward a URL."""
    event = delivery["payload"]
    subject, task_id = delivery["user_subject"], delivery["task_id"]
    if not instance or not event.get("artifactPairProof") or event.get("eventType") != "task.artifact.ready":
        return {"allowed": False}
    task = service.tasks.get_task(task_id, user_subject=subject)
    if (
        task["agent_host"] != "nanobot"
        or task["origin_endpoint_id"] != endpoint["endpoint_id"]
        or task["active_conversation_ref"] != endpoint["conversation_ref"]
    ):
        return {"allowed": False}
    with service.tasks._connect() as db:
        ownership = endpoint_instances(db, subject)
        if ownership.get(endpoint["endpoint_id"]) != ("nanobot", instance):
            return {"allowed": False}
        proof = origin_pair_proof(db, subject, endpoint, ownership)
    if proof != event["artifactPairProof"]:
        return {"allowed": False}
    payload = event["payload"]
    try:
        service.require_task_result_access(user_subject=subject, task_id=task_id)
        artifact = service.tasks.get_artifact(
            task_id=task_id, user_subject=subject, artifact_id=payload["artifactId"], include_source_ref=True
        )
        if (
            artifact["state"] != "ready"
            or artifact["artifact_type"] not in ARTIFACT_TYPES
            or artifact["artifact_type"] != payload["artifactType"]
            or artifact["download_url"] != payload["downloadUrl"]
            or artifact["expires_at"] != payload["expiresAt"]
        ):
            return {"allowed": False}
        if artifact["artifact_type"] == "database_csv":
            result = _database_csv(service, subject, task_id, artifact, with_body)
        else:
            record = service.document_downloads.get(artifact["source_ref"], include_document=True)
            if (
                record["user_subject"] != subject
                or record["state"] != "ready"
                or record["document_type"] not in DOCUMENT_TYPES[artifact["artifact_type"]]
                or record["expires_at"] != artifact["expires_at"]
                or record["card_url"] + "/file" != artifact["download_url"]
            ):
                return {"allowed": False}
            service.document_downloads.require_access(record)
            result = (
                service.document_downloads.ready_payload(artifact["source_ref"], user_subject=subject)
                if with_body
                else {}
            )
        response = {"allowed": True, "artifactId": artifact["artifact_id"]}
        if with_body:
            body = result["body"]
            mime = artifact["content_type"].split(";", 1)[0]
            if (
                mime not in {"application/pdf", "text/csv"}
                or len(body) != artifact["byte_size"]
                or result["filename"] != artifact["filename"]
                or result["content_type"].split(";", 1)[0] != mime
            ):
                return {"allowed": False}
            response["file"] = {
                "filename": artifact["filename"],
                "contentType": mime,
                "size": len(body),
                "contentBase64": base64.b64encode(body).decode("ascii"),
            }
        return response
    except (
        KeyError,
        PermissionError,
        ValueError,
        DocumentDownloadStateError,
        DocumentDownloadAccessDenied,
        DocumentDownloadIntegrityError,
    ):
        return {"allowed": False}
