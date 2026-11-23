"""Additional data only for the isolated skill/channel/file Workspace acceptance."""

import sqlite3

from tests.test_workspace_skill_trial import PROPOSAL


def seed(fixture):
    service = fixture.service
    a = fixture.subjects["a"]
    with sqlite3.connect(service.db_path) as db:
        db.execute("""INSERT OR IGNORE INTO agent_host_compatibility VALUES
            ('nanobot','0.3.0','L3','isolated-candidate-conformance','isolated-fixture',CURRENT_TIMESTAMP)""")
        level = db.execute("""SELECT accepted_level FROM agent_host_compatibility
            WHERE implementation_name='nanobot' AND implementation_version='0.3.0'""").fetchone()
        assert level and level[0] == "L3"
    service.tasks.ensure_endpoint(
        user_subject=a,
        token_id=fixture.tokens["a"],
        agent_host="openclaw",
        endpoint_key="test-original-telegram-501",
        client_type="telegram",
        external_subject="501",
        capabilities=["timeline_message", "direct_status"],
        conversation_ref="agent:main:telegram:direct:501",
        route={"channel": "telegram", "to": "501", "accountId": "default"},
    )
    service.skills.save(
        "user:" + a,
        {"oa-work-log": {"profiles": ["preview"]}},
        expected_revision=0,
        actor="isolated-test",
        reason="per-user skill test",
    )
    service.skill_authoring.save(
        a, proposal=PROPOSAL, request_key="isolated-browser-sample"
    )


def start_media(fixture, port):
    from agentbridge.auth.card import TrustedAuthApplication
    from agentbridge.auth.document_download import TrustedDocumentDownloadApplication
    from agentbridge.auth.server import (
        create_auth_http_server,
        validate_auth_server_config,
    )
    from agentbridge.auth.timeline_attachment import (
        TrustedTimelineAttachmentApplication,
    )
    from tests.support.interaction_acceptance import SyntheticBroker

    service = fixture.service

    def fetch(record):
        if (
            record["filename"] != "合成证书.pdf"
            or record["document_type"] != "patent_certificate"
        ):
            raise PermissionError("Only a synthetic seeded file is available")
        return {
            "body": PDF,
            "filename": record["filename"],
            "content_type": "application/pdf",
        }

    service.fetch_document_download = fetch
    return create_auth_http_server(
        config=validate_auth_server_config(
            host="127.0.0.1",
            port=port,
            public_base_url=f"http://127.0.0.1:{port}",
            tls_cert=None,
            tls_key=None,
        ),
        application=TrustedAuthApplication(
            challenge_store=service.challenges,
            broker=SyntheticBroker(service.challenges),
        ),
        download_application=TrustedDocumentDownloadApplication(
            download_store=service.document_downloads, fetcher=fetch
        ),
        timeline_attachment_application=TrustedTimelineAttachmentApplication(
            attachment_store=service.timeline_attachments
        ),
    )


def seed_files(service, subject):
    """Attach fixture files to the actual signed-in user's most recent ordinary accepted read."""
    from tests.authorization_fixtures import grant_permissions
    from tests.support.file_retention_acceptance import seed_retained_csv
    from tests.test_document_downloads import _reference

    with service.workspace._connect() as db:
        row = db.execute(
            """SELECT t.task_id FROM agent_tasks t JOIN agent_host_dispatches d
            ON d.accepted_run_id=t.host_task_key AND d.user_subject=t.user_subject AND d.origin_endpoint_id=t.origin_endpoint_id
            WHERE t.agent_host='nanobot' AND t.user_subject=? AND t.status='succeeded'
            AND NOT EXISTS (SELECT 1 FROM skill_task_bindings s WHERE s.task_id=t.task_id)
            ORDER BY t.created_at DESC LIMIT 1""",
            (subject,),
        ).fetchone()
    if not row:
        raise ValueError("先用本人 NanoBot 工作台发送一次普通待办查询。")
    task_id = row["task_id"]
    previous = service.tasks.list_artifacts(task_id=task_id, user_subject=subject)
    if any(item["artifact_type"] == "certificate_scan" for item in previous):
        return task_id
    grants = service.user_grants.get(subject)
    grant_permissions(
        service.user_grants,
        subject,
        sorted(
            set(grants["permissions"])
            | {"oa.certificate.read", "oa.certificate.download"}
        ),
    )
    session = service.sessions.find(user_subject=subject, system_id="oa")
    grant = service.document_downloads.create(
        user_subject=subject,
        system_id="oa",
        session_id=session["session_id"],
        document=_reference(),
        filename="合成证书.pdf",
        document_type="patent_certificate",
        display_size="合成文件",
        card_base_url=service.trusted_card_base_url,
    )
    artifact, _ = service.tasks.link_artifact(
        task_id=task_id,
        user_subject=subject,
        artifact={
            "artifact_type": "certificate_scan",
            "source_ref": grant["download_id"],
            "filename": "合成证书.pdf",
            "content_type": "application/pdf",
            "byte_size": len(PDF),
            "download_url": grant["card_url"] + "/file",
            "expires_at": grant["expires_at"],
        },
    )
    with service.tasks._connect() as db:
        db.execute(
            "UPDATE task_artifacts SET state='expired',expires_at='2001-01-01T00:00:00+00:00' WHERE artifact_id=?",
            (artifact["artifact_id"],),
        )
    seed_retained_csv(service, user_subject=subject, task_id=task_id)
    return task_id


def synthetic_pdf():
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for idx, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(idx).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 4\n0000000000 65535 f \n" + b"".join(
        b"%010d 00000 n \n" % v for v in offsets[1:]
    )
    return (
        data
        + b"trailer\n<< /Root 1 0 R /Size 4 >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )


PDF = synthetic_pdf()


def attach_file_fixture(server, fixture, origin):
    from http import HTTPStatus
    from urllib.parse import urlsplit

    cls = server.RequestHandlerClass
    parent = cls.do_POST

    def post(self):
        if urlsplit(self.path).path != "/fixture-file":
            return parent(self)
        account = self._authenticate(require_csrf=True)
        if not account:
            return
        if (
            account["user_subject"] != fixture.subjects["a"]
            or self.headers.get("Origin") != origin
            or self.headers.get("Host") != urlsplit(origin).netloc
        ):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            task = seed_files(fixture.service, account["user_subject"])
            self._json(200, {"ready": True, "taskId": task})
        except ValueError:
            self._json(409, {"ready": False})

    cls.do_POST = post
    write_download = cls._download

    def download(self, item):
        write_download(self, item)
        import re

        if not re.fullmatch(
            r"/api/database/reports/[a-z][a-z0-9_-]{0,63}/[a-f0-9]{32}/download",
            urlsplit(self.path).path,
        ):
            return
        import hashlib
        import json
        import os
        from datetime import UTC, datetime

        from agentbridge.workspace.server import SESSION_COOKIE

        account = fixture.service.workspace.verify_session(
            self._cookie(SESSION_COOKIE), touch=False
        )
        if not account:
            return
        receipt = {
            "time": datetime.now(UTC).isoformat(),
            "subject": next(
                (
                    label
                    for label, subject in fixture.subjects.items()
                    if subject == account["user_subject"]
                ),
                None,
            ),
            "status": 200,
            "bytes": len(item["body"]),
            "digest": hashlib.sha256(item["body"]).hexdigest()[:16],
        }
        fd = os.open(
            fixture.home.parent / "fixture-downloads.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(receipt) + "\n")

    cls._download = download
