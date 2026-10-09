from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.timeline_attachments import TimelineAttachmentIntegrityError
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.stores import WorkspaceConflictError
from tests.test_workspace import FakeGateway, _create_account, _service


class WorkspaceAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        key = Path(self.temp.name) / "session.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        environment = patch.dict(os.environ, {"AGENTBRIDGE_SESSION_KEY_FILE": str(key)})
        environment.start()
        self.addCleanup(environment.stop)
        self.service = _service(self.temp.name)
        self.account = _create_account(
            self.service,
            user_subject="user-a",
            username="alice",
            endpoint_key="telegram:*:alice",
        )
        self.service.tasks.ensure_endpoint(
            user_subject="user-a",
            token_id="token-alice",
            agent_host="openclaw",
            endpoint_key="telegram:*:alice",
            client_type="telegram",
            external_subject="alice",
            conversation_ref="agent:main:telegram:direct:alice",
            capabilities=["direct_status", "timeline_message"],
            route={"channel": "telegram", "to": "alice"},
        )
        self.gateway = FakeGateway()
        self.app = WorkspaceApplication(service=self.service, gateway=self.gateway)
        self.addCleanup(self.app.close)
        worker = patch.object(self.app, "_ensure_dispatch_worker")
        self.wake = worker.start()
        self.addCleanup(worker.stop)

    def send(self, *, key="request-1", text="read the image", images=None):
        return self.app.send_chat_stream(
            self.account,
            message=text,
            idempotency_key=key,
            attachments=images,
        )

    def counts(self):
        with sqlite3.connect(self.service.db_path) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "user_timeline", "notification_outbox", "timeline_attachments",
                    "agent_host_dispatches", "agent_host_dispatch_events",
                )
            }

    def files(self):
        return sorted(self.service.timeline_attachments.cache_dir.iterdir())

    def seed_queue(self):
        for ordinal in range(3):
            self.service.workspace.create_host_dispatch(
                account_id=self.account["account_id"],
                user_subject="user-a",
                agent_host="openclaw",
                host_binding_ref=self.account["endpoint_key"],
                origin_endpoint_id=self.account["endpoint_id"],
                conversation_ref=self.account["openclaw_session_key"],
                message_key=f"queue-{ordinal}",
                payload_hash="a" * 64,
                idempotency_key=f"queue-{ordinal}",
            )

    def test_worker_wakes_only_after_message_media_outbox_and_dispatch_commit(self):
        expected = dict.fromkeys(self.counts(), 1)
        self.wake.side_effect = lambda _account_id: self.assertEqual(self.counts(), expected)

        self.send(images=[_image()])

        self.assertEqual(self.counts(), expected)
        self.wake.assert_called_once_with(self.account["account_id"])
        self.assertEqual(self.gateway.calls, [])

    def test_each_acceptance_boundary_rolls_back_all_rows_and_new_files(self):
        boundaries = (
            (self.service.timeline_attachments, "_create_many_in_connection"),
            (self.service.workspace, "_create_host_dispatch_in_connection"),
            (self.service.tasks, "_append_timeline_message_in_connection"),
        )
        empty = self.counts()
        for store, method in boundaries:
            with self.subTest(boundary=method):
                original = getattr(store, method)

                def fail_after_write(*args, **kwargs):
                    original(*args, **kwargs)
                    # A second connection must not observe any uncommitted row.
                    self.assertEqual(self.counts(), empty)
                    raise RuntimeError("injected acceptance interruption")

                with patch.object(store, method, side_effect=fail_after_write):
                    with self.assertRaisesRegex(RuntimeError, "injected acceptance"):
                        self.send(images=[_image()])
                self.assertEqual(self.counts(), empty)
                self.assertEqual(self.files(), [])
        self.wake.assert_not_called()
        self.send(images=[_image()])
        self.assertTrue(all(value == 1 for value in self.counts().values()))

    def test_queue_rejection_leaves_no_message_outbox_or_attachment(self):
        self.seed_queue()
        before = self.counts()

        with self.assertRaisesRegex(WorkspaceConflictError, "QUEUE_CONVERSATION_LIMIT"):
            self.send(images=[_image()])

        self.assertEqual(self.counts(), before)
        self.assertEqual(self.files(), [])
        self.wake.assert_not_called()

    def test_same_key_retry_reuses_message_dispatch_media_and_outbox(self):
        self.send(images=[_image()])
        before = self.counts()
        files = self.files()
        dispatch = self.service.workspace.list_host_dispatches(user_subject="user-a")[0]

        self.send(images=[_image()])

        self.assertEqual(self.counts(), before)
        self.assertEqual(self.files(), files)
        self.assertEqual(
            self.service.workspace.list_host_dispatches(user_subject="user-a")[0]["dispatch_id"],
            dispatch["dispatch_id"],
        )

    def test_completed_retry_survives_full_queue_and_expired_attachment(self):
        self.send(images=[_image()])
        with sqlite3.connect(self.service.db_path) as connection:
            connection.execute("UPDATE agent_host_dispatches SET state = 'completed'")
            connection.execute("UPDATE timeline_attachments SET state = 'expired'")
        self.seed_queue()
        before = self.counts()

        self.send(images=[_image()])

        self.assertEqual(self.counts(), before)

    def test_changed_text_or_attachment_set_cannot_mutate_accepted_request(self):
        self.send(images=[_image()])
        before = self.counts()
        files = self.files()
        variants = (
            {"text": "different text", "images": [_image()]},
            {"images": [_image(body=b"different")]},
            {"images": [_image(name="changed.png")]},
            {"images": []},
            {"images": [_image(), _image(name="second.png")]},
        )
        for values in variants:
            with self.subTest(values=values):
                with self.assertRaises((WorkspaceConflictError, TimelineAttachmentIntegrityError)):
                    self.send(**values)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.files(), files)
        self.assertEqual(files[0].read_bytes(), b"\x89PNG\r\n\x1a\noriginal")

    def test_adding_attachment_to_text_only_retry_cleans_only_new_files(self):
        self.send()
        before = self.counts()

        with self.assertRaisesRegex(WorkspaceConflictError, "IDEMPOTENCY_PAYLOAD_MISMATCH"):
            self.send(images=[_image()])

        self.assertEqual(self.counts(), before)
        self.assertEqual(self.files(), [])

    def test_legacy_orphan_message_must_match_before_dispatch_can_be_attached(self):
        self.service.tasks.append_timeline_message(
            user_subject="user-a",
            source_endpoint_id=self.account["endpoint_id"],
            message_key="workspace:user:request-1",
            role="user",
            text="original orphan message",
            payload={"attachments": []},
        )
        before = self.counts()

        with self.assertRaisesRegex(WorkspaceConflictError, "IDEMPOTENCY_PAYLOAD_MISMATCH"):
            self.send(text="replacement text", images=[_image()])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.files(), [])

        self.send(text="original orphan message")
        after = self.counts()
        self.assertEqual(after["user_timeline"], 1)
        self.assertEqual(after["notification_outbox"], 1)
        self.assertEqual(after["agent_host_dispatches"], 1)

    def test_failure_with_legacy_media_preserves_reused_files(self):
        media = self.service.timeline_attachments.create_many(
            user_subject="user-a",
            message_key="workspace:user:request-1",
            attachments=[_image()],
            media_base_url=self.service.trusted_card_base_url,
        )
        before = self.counts()
        files = self.files()
        with patch.object(
            self.service.tasks, "_append_timeline_message_in_connection",
            side_effect=RuntimeError("injected acceptance interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self.send(images=[_image()])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.files(), files)
        self.assertTrue(self.service.timeline_attachments.ready_payload(media[0]["attachment_id"])["body"])

    def test_concurrent_same_key_is_one_atomic_request(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.send, images=[_image()]) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        self.assertTrue(all(value == 1 for value in self.counts().values()))
        self.assertEqual(len(self.files()), 1)

    def test_two_users_with_same_key_keep_messages_dispatches_and_media_separate(self):
        account_b = _create_account(
            self.service,
            user_subject="user-b",
            username="bob",
            endpoint_key="telegram:*:bob",
        )
        self.send(images=[_image()])
        self.app.send_chat_stream(
            account_b,
            message="Bob's image",
            idempotency_key="request-1",
            attachments=[_image(body=b"bob-image")],
        )
        before = self.counts()
        self.app.send_chat_stream(
            account_b,
            message="Bob's image",
            idempotency_key="request-1",
            attachments=[_image(body=b"bob-image")],
        )
        self.assertEqual(self.counts(), before)
        dispatch_a = self.service.workspace.list_host_dispatches(user_subject="user-a")
        dispatch_b = self.service.workspace.list_host_dispatches(user_subject="user-b")
        self.assertEqual(len(dispatch_a), 1)
        self.assertEqual(len(dispatch_b), 1)
        self.assertNotEqual(dispatch_a[0]["dispatch_id"], dispatch_b[0]["dispatch_id"])
        with sqlite3.connect(self.service.db_path) as connection:
            messages = dict(connection.execute("SELECT user_subject, text FROM user_timeline"))
            media = dict(connection.execute("SELECT user_subject, attachment_id FROM timeline_attachments"))
        self.assertEqual(messages, {"user-a": "read the image", "user-b": "Bob's image"})
        self.assertNotEqual(media["user-a"], media["user-b"])
        self.assertEqual(
            self.service.timeline_attachments.ready_payload(media["user-a"])["body"],
            b"\x89PNG\r\n\x1a\noriginal",
        )
        self.assertEqual(
            self.service.timeline_attachments.ready_payload(media["user-b"])["body"],
            b"\x89PNG\r\n\x1a\nbob-image",
        )

    def test_maintenance_cleans_only_unreferenced_owned_attachment_names(self):
        self.send(images=[_image()])
        committed = self.files()[0]
        cache = self.service.timeline_attachments.cache_dir
        # These represent the cache remainder of a hard exit before SQL commit.
        orphan = cache / secrets.token_urlsafe(32)
        temporary = cache / (secrets.token_urlsafe(32) + ".tmp")
        unrelated = cache / "keep-notes.txt"
        for path in (orphan, temporary, unrelated):
            path.write_bytes(b"fixture")

        self.assertEqual(self.service.timeline_attachments.prune_expired(), 0)

        self.assertTrue(committed.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(temporary.exists())

    def test_hard_exit_before_commit_rolls_back_database_and_orphan_is_reclaimed(self):
        script = """
import os, sys
from unittest.mock import patch
from tests.test_workspace import FakeGateway, _service
from agentbridge.workspace.application import WorkspaceApplication
service = _service(sys.argv[1])
account = service.workspace.get_account(sys.argv[2])
app = WorkspaceApplication(service=service, gateway=FakeGateway())
original = service.tasks._append_timeline_message_in_connection
def exit_after_all_writes(connection, **kwargs):
    original(connection, **kwargs)
    for table in ('user_timeline', 'notification_outbox', 'timeline_attachments',
                  'agent_host_dispatches', 'agent_host_dispatch_events'):
        assert connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
    os._exit(74)
with patch.object(service.tasks, '_append_timeline_message_in_connection', exit_after_all_writes):
    app.send_chat_stream(account, message='read the image', idempotency_key='request-1',
                         attachments=[__import__('json').loads(sys.argv[3])])
"""
        before = self.counts()
        result = subprocess.run(
            [sys.executable, "-c", script, self.temp.name,
             self.account["account_id"], json.dumps(_image())],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 74, result.stderr)
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.files()), 1)

        self.service.timeline_attachments.prune_expired()

        self.assertEqual(self.files(), [])
        self.send(images=[_image()])
        self.assertTrue(all(value == 1 for value in self.counts().values()))

    def test_public_payload_hash_format_remains_compatible(self):
        import hashlib

        self.send(images=[_image()])
        with sqlite3.connect(self.service.db_path) as connection:
            connection.row_factory = sqlite3.Row
            attachment = connection.execute("SELECT * FROM timeline_attachments").fetchone()
            dispatch = connection.execute("SELECT * FROM agent_host_dispatches").fetchone()
        canonical = {
            "message": "read the image",
            "attachments": [{
                "attachmentId": attachment["attachment_id"],
                "contentHash": attachment["content_hash"],
                "mimeType": attachment["content_type"],
                "fileName": attachment["filename"],
            }],
        }
        legacy_hash = hashlib.sha256(json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(dispatch["payload_hash"], legacy_hash)


def _image(*, body=b"original", name="image.png"):
    return {
        "type": "image",
        "mimeType": "image/png",
        "fileName": name,
        "content": base64.b64encode(b"\x89PNG\r\n\x1a\n" + body).decode("ascii"),
    }
