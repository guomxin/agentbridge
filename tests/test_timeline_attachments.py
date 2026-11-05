from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import unittest

from agentbridge.auth.timeline_attachment import TrustedTimelineAttachmentApplication
from agentbridge.core.timeline_attachments import (
    TimelineAttachmentExpired,
    TimelineAttachmentIntegrityError,
    TimelineAttachmentStore,
    public_attachment,
)


class TimelineAttachmentStoreTests(unittest.TestCase):
    def test_persists_ordered_images_and_reuses_idempotent_message(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TimelineAttachmentStore(Path(tmp) / "agentbridge.db")
            first = _image("first.png", b"first")
            second = _image("second.png", b"second")

            created = store.create_many(
                user_subject="user-a",
                message_key="workspace:user:message-1",
                attachments=[first, second],
                media_base_url="https://10.10.50.213:8780",
            )
            reused = store.create_many(
                user_subject="user-a",
                message_key="workspace:user:message-1",
                attachments=[first, second],
                media_base_url="https://10.10.50.213:8780",
            )

            self.assertEqual(
                [item["attachment_id"] for item in created],
                [item["attachment_id"] for item in reused],
            )
            self.assertEqual([item["ordinal"] for item in created], [0, 1])
            self.assertRegex(
                created[0]["media_url"],
                r"^https://10\.10\.50\.213:8780/media/.+/file$",
            )
            self.assertEqual(
                store.ready_payload(created[1]["attachment_id"])["body"],
                b"second",
            )
            public = public_attachment(created[0])
            self.assertNotIn("user_subject", public)
            self.assertNotIn("content_hash", public)
            self.assertEqual(public["mimeType"], "image/png")

    def test_rejects_changed_content_for_same_message_key(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TimelineAttachmentStore(Path(tmp) / "agentbridge.db")
            store.create_many(
                user_subject="user-a",
                message_key="workspace:user:message-1",
                attachments=[_image("image.png", b"first")],
                media_base_url="https://10.10.50.213:8780",
            )

            with self.assertRaises(TimelineAttachmentIntegrityError):
                store.create_many(
                    user_subject="user-a",
                    message_key="workspace:user:message-1",
                    attachments=[_image("image.png", b"changed")],
                    media_base_url="https://10.10.50.213:8780",
                )

    def test_expired_attachment_is_removed_and_returns_gone(self) -> None:
        with TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 10, tzinfo=timezone.utc)
            clock = [now]
            store = TimelineAttachmentStore(
                Path(tmp) / "agentbridge.db",
                clock=lambda: clock[0],
            )
            created = store.create_many(
                user_subject="user-a",
                message_key="workspace:user:message-1",
                attachments=[_image("image.png", b"body")],
                media_base_url="https://10.10.50.213:8780",
                ttl_seconds=300,
            )[0]
            clock[0] = now + timedelta(seconds=301)

            self.assertEqual(store.prune_expired(), 1)
            with self.assertRaises(TimelineAttachmentExpired):
                store.ready_payload(created["attachment_id"])

    def test_public_metadata_is_owned_and_marks_expiry_without_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = [datetime(2026, 11, 5, tzinfo=timezone.utc)]
            store = TimelineAttachmentStore(Path(tmp) / "agentbridge.db", clock=lambda: clock[0])
            created = store.create_many(user_subject="user-a", message_key="metadata",
                attachments=[_image("原图.png", b"body")],
                media_base_url="https://10.10.50.213:8780", ttl_seconds=300)[0]
            aid = created["attachment_id"]
            before = store.public_metadata(user_subject="user-a", attachment_ids=[aid])[aid]
            self.assertEqual(before["state"], "ready")
            self.assertEqual(before["expiresAt"], created["expires_at"])
            self.assertEqual(store.public_metadata(user_subject="user-b", attachment_ids=[aid]), {})
            clock[0] += timedelta(seconds=301)
            after = store.public_metadata(user_subject="user-a", attachment_ids=[aid])[aid]
            self.assertEqual(after["state"], "expired")
            self.assertIsNone(after["mediaUrl"])
            self.assertEqual(after["fileName"], "原图.png")
            self.assertNotIn("user_subject", after)
            self.assertNotIn("body", after)
            self.assertTrue(store._cache_path(aid).is_file())
            with store._connect() as db:
                self.assertEqual(db.execute("SELECT state FROM timeline_attachments WHERE attachment_id=?", (aid,)).fetchone()[0], "ready")

    def test_trusted_http_application_serves_inline_image(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TimelineAttachmentStore(Path(tmp) / "agentbridge.db")
            created = store.create_many(
                user_subject="user-a",
                message_key="workspace:user:message-1",
                attachments=[_image("中文图片.png", b"body")],
                media_base_url="https://10.10.50.213:8780",
            )[0]
            application = TrustedTimelineAttachmentApplication(
                attachment_store=store
            )

            response = application.get_file(created["attachment_id"])

            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertIn("inline", response.headers["Content-Disposition"])
            self.assertEqual(response.body, b"body")


def _image(filename: str, body: bytes) -> dict:
    return {
        "type": "image",
        "mimeType": "image/png",
        "fileName": filename,
        "content": base64.b64encode(body).decode("ascii"),
    }


if __name__ == "__main__":
    unittest.main()
