"""Receive media only from the verified sender and the pinned Telegram file origin."""

import base64
import http.client
import re
import time
from pathlib import PurePosixPath

from agentbridge.core.chat_mirror import public_mirror_text
from .media import DOCUMENT_FORMATS, FORMATS, MAX_FILE
from .telegram_timeline import telegram_text_fits

MEDIA_FIELDS = (
    "photo",
    "document",
    "voice",
    "audio",
    "video",
    "video_note",
    "animation",
    "sticker",
)
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,512}")
FILE_PATH = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+\.[A-Za-z0-9]{1,12}")
UNSUPPORTED = "目前可发送照片、截图、PNG/JPEG/WebP 图片、文本文件或含文字的 PDF。语音可先转成文字。"
UNREADABLE = "附件读取失败或超过 6 MiB，请缩小后重新发送。"


class MediaError(ValueError):
    pass


def _select(msg):
    if "photo" in msg:
        sizes = msg["photo"]
        if not isinstance(sizes, list) or not sizes or len(sizes) > 16:
            raise MediaError(UNREADABLE)
        choices = [
            x
            for x in sizes
            if isinstance(x, dict)
            and all(
                type(x.get(k)) is int and 0 < x[k] <= 65535 for k in ("width", "height")
            )
            and (
                x.get("file_size") is None
                or type(x["file_size"]) is int
                and 0 < x["file_size"] <= MAX_FILE
            )
        ]
        if not choices:
            raise MediaError(UNREADABLE)
        return max(choices, key=lambda x: x["width"] * x["height"]), "image/jpeg"
    sticker = msg.get("sticker")
    if (
        isinstance(sticker, dict)
        and sticker.get("is_animated") is False
        and sticker.get("is_video") is False
    ):
        return sticker, "image/webp"
    document = msg.get("document")
    if isinstance(document, dict):
        mime = document.get("mime_type")
        if mime in ("application/octet-stream", None):
            ext = PurePosixPath(str(document.get("file_name", ""))).suffix.lower()
            mime = next(
                (
                    m
                    for m, (s, _) in {**FORMATS, **DOCUMENT_FORMATS}.items()
                    if s == ext
                ),
                None,
            )
        if isinstance(mime, str) and (mime in FORMATS or mime in DOCUMENT_FORMATS):
            return document, mime
    raise MediaError(UNSUPPORTED)


def download(api, media):
    """Both Bot API operations use a fixed origin. Never follow redirects or echo credentials."""
    identifier = media.get("file_id")
    if not isinstance(identifier, str) or not FILE_ID.fullmatch(identifier):
        raise MediaError(UNREADABLE)
    limit = MAX_FILE
    size = media.get("file_size")
    if size is not None and (type(size) is not int or not 0 < size <= limit):
        raise MediaError(UNREADABLE)
    ref = api.call("getFile", file_id=identifier)
    path = ref.get("file_path") if isinstance(ref, dict) else None
    if (
        not isinstance(path, str)
        or len(path) > 512
        or not FILE_PATH.fullmatch(path)
        or ref.get("file_id") != identifier
        or (
            ref.get("file_size") is not None
            and (
                type(ref["file_size"]) is not int
                or not 0 < ref["file_size"] <= limit
                or size is not None
                and ref["file_size"] != size
            )
        )
    ):
        raise MediaError(UNREADABLE)
    conn = api._connect()
    try:
        conn.request("GET", f"/file/bot{api._token}/{path}")
        response = conn.getresponse()
        if response.status != 200:
            raise MediaError(UNREADABLE)
        header = response.getheader("Content-Length")
        if header is not None and (
            not re.fullmatch(r"[0-9]{1,8}", header) or not 0 < int(header) <= limit
        ):
            raise MediaError(UNREADABLE)
        start = time.monotonic()
        raw = bytearray()
        while len(raw) <= limit:
            if time.monotonic() - start > 15:
                raise MediaError(UNREADABLE)
            chunk = response.read1(min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if (
            not raw
            or len(raw) > limit
            or any(
                s is not None and int(s) != len(raw)
                for s in (size, ref.get("file_size"), header)
            )
        ):
            raise MediaError(UNREADABLE)
        return bytes(raw)
    finally:
        conn.close()


def receive(pilot, msg, chat, message):
    binding, digest = pilot._binding(chat)
    if binding is None:
        return
    with pilot.db:
        if not pilot.db.execute(
            "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,'reading_media',NULL,NULL)",
            (pilot.bot_id, chat, message, binding["user_subject"], digest),
        ).rowcount:
            return
    caption = msg.get("caption", "")
    if (
        "reply_to_message" in msg
        or not isinstance(caption, str)
        or (
            caption
            and (
                not telegram_text_fits(caption)
                or caption.lstrip().startswith("/")
                or public_mirror_text(caption) is None
            )
        )
    ):
        pilot.deliver(
            chat, message, "请单独发送附件和普通说明，需要确认的操作请回到原任务。"
        )
        return
    try:
        item, mime = _select(msg)
        blob = download(pilot.api, item)
        formats = FORMATS if mime in FORMATS else DOCUMENT_FORMATS
        suffix, signature = formats[mime]
        if (
            not blob.startswith(signature)
            or mime == "image/webp"
            and blob[8:12] != b"WEBP"
        ):
            raise MediaError(UNREADABLE)
        if mime in DOCUMENT_FORMATS and mime != "application/pdf":
            try:
                value = blob.decode("utf-8-sig")
            except UnicodeError:
                raise MediaError("请使用 UTF-8 文本文件或含文字的 PDF。") from None
            if not value.strip() or any(
                ord(c) < 32 and c not in "\n\r\t" for c in value
            ):
                raise MediaError(UNREADABLE)
    except MediaError as exc:
        pilot.deliver(chat, message, str(exc))
        return
    except (OSError, http.client.HTTPException, RuntimeError):
        pilot.deliver(chat, message, UNREADABLE)
        return
    binding, fresh = pilot._binding(chat)
    if not binding or fresh != digest:
        pilot._state(chat, message, "binding_changed")
        return
    is_image = mime in FORMATS
    attachment = {
        "type": "image" if is_image else "document",
        "mimeType": mime,
        "fileName": "attachment" + suffix,
        "content": base64.b64encode(blob).decode("ascii"),
    }
    text = caption.strip() or (
        "请查看我发送的图片。" if is_image else "请阅读我发送的文档。"
    )
    with pilot.db:
        pilot.timeline.queue(chat, message, "user", text)
        pilot._state(chat, message, "accepting")
    pilot._dispatch_ordinary(chat, message, binding, text, [attachment])
