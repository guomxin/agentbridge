from __future__ import annotations

from urllib.parse import quote

from agentbridge.auth.card import AuthCardResponse
from agentbridge.core.timeline_attachments import (
    TimelineAttachmentExpired,
    TimelineAttachmentIntegrityError,
    TimelineAttachmentNotFound,
    TimelineAttachmentStore,
)


class TrustedTimelineAttachmentApplication:
    def __init__(self, *, attachment_store: TimelineAttachmentStore) -> None:
        self.attachment_store = attachment_store

    def get_file(self, attachment_id: str) -> AuthCardResponse:
        try:
            payload = self.attachment_store.ready_payload(attachment_id)
        except TimelineAttachmentNotFound:
            return _message_response(404, "图片不存在")
        except TimelineAttachmentExpired:
            return _message_response(410, "图片已过期")
        except TimelineAttachmentIntegrityError:
            return _message_response(409, "图片校验失败")

        filename = str(payload["filename"])
        return AuthCardResponse(
            200,
            {
                "Cache-Control": "private, max-age=300",
                "Content-Disposition": (
                    "inline; filename=timeline-image; "
                    f"filename*=UTF-8''{quote(filename, safe='')}"
                ),
                "Content-Type": str(payload["content_type"]),
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
            payload["body"],
        )


def _message_response(status: int, message: str) -> AuthCardResponse:
    body = message.encode("utf-8")
    return AuthCardResponse(
        status,
        {
            "Cache-Control": "no-store",
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
        body,
    )
