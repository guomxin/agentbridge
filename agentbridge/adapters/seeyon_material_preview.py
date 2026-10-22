"""On-demand reads of file references actually exposed by a current OA item.

Only closed, observed GET contracts are accepted. References are resolved again
inside the original account's worker; source URLs never leave the adapter.
This does not mark evidence reviewed or expand the approval material scope.
"""
from __future__ import annotations

import base64
from datetime import date
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import json
import re
import stat
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from zipfile import BadZipFile, ZipFile


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_EXPANDED = 24 * 1024 * 1024
MAX_ARCHIVE_ITEMS = 40
MAX_REFERENCES = 128
_ID = re.compile(r"-?[0-9]{1,20}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_UNSUPPORTED = "此材料格式或读取地址暂不支持预览，请在 OA 中查看。"


class MaterialPreviewError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _fail(code, message, status=409):
    raise MaterialPreviewError(code, message, status)


def _origin(value):
    parsed = urlsplit(value)
    return (parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


def _canonical_reference(value, *, base_url="", image=False):
    """Return a relative route only for closed read contracts, never free URLs."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        parsed = urlsplit(unescape(value))
        if parsed.fragment or parsed.username or parsed.password or value.startswith("//"):
            return None
        if parsed.scheme or parsed.netloc:
            if not base_url or parsed.scheme not in {"http", "https"} or _origin(value) != _origin(base_url):
                return None
        elif not value.startswith("/"):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        query = dict(pairs)
        if len(query) != len(pairs):
            return None
        if parsed.path == "/seeyon/fileUpload.do":
            if (set(query) != {"method", "type", "fileId", "createDate"}
                    or query["method"] != "showRTE" or query["type"] != "image"
                    or not _ID.fullmatch(query["fileId"]) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", query["createDate"])):
                return None
            date.fromisoformat(query["createDate"])
            kind = "image"
        elif parsed.path == "/seeyon/fileDownload.do":
            if (set(query) != {"method", "viewMode", "fileId", "createDate", "filename", "v"}
                    or query["method"] != "download" or query["viewMode"] != "download"
                    or not _ID.fullmatch(query["fileId"]) or not re.fullmatch(r"[0-9]{1,10}", query["v"])
                    or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", query["createDate"])
                    or not 1 <= len(query["filename"]) <= 500):
                return None
            date.fromisoformat(query["createDate"])
            extension = query["filename"].rsplit(".", 1)[-1].lower()
            kind = "pdf" if extension == "pdf" else "image" if extension in {"png", "jpg", "jpeg"} else "file"
            if kind == "file" or (image and kind != "image"):
                return None
        elif not image and re.fullmatch(r"/seeyon/rest/attachment/file/batchDownload/-?[0-9]{1,20}(?:,-?[0-9]{1,20}){0,39}", parsed.path):
            if set(query) != {"zipFileName"} or not 1 <= len(query["zipFileName"]) <= 500:
                return None
            kind = "archive"
        else:
            return None
        return kind, parsed.path + "?" + urlencode(sorted(query.items()))
    except (ValueError, UnicodeError):
        return None


def _reference(href, name, *, base_url="", image=False):
    parsed = _canonical_reference(href, base_url=base_url, image=image)
    kind, route = parsed if parsed else ("image" if image else "file", "")
    name = str(name or ("正文图片" if image else "未命名附件"))[:500]
    identity = sha256(json.dumps([kind, route or str(href), name], ensure_ascii=False).encode()).hexdigest()
    public = {"id": identity, "name": name, "kind": kind,
              "state": "available" if parsed else "unavailable"}
    if not parsed:
        public["reason"] = _UNSUPPORTED
    return {"public": public, "route": route}


class _Images(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.frames = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (bool(self.frames and self.frames[-1][1])
                  or tag in {"script", "style", "template", "noscript", "svg", "head"}
                  or "hidden" in attrs or attrs.get("aria-hidden") == "true"
                  or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style") or "", re.I)))
        if not hidden and tag == "img" and len(self.items) < MAX_REFERENCES:
            self.items.append((attrs.get("src", ""), attrs.get("alt") or "正文图片"))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.frames.append((tag, hidden))

    def handle_endtag(self, tag):
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index][0] == tag:
                self.frames = self.frames[:index]
                break


def image_references(text, *, base_url="", only_file_routes=False):
    if not isinstance(text, str) or len(text) > 1048576:
        return []
    value = text
    # OA rich-text fields can be entity-encoded inside a rendered form.
    for _ in range(2):
        if "<img" in value.lower():
            break
        changed = unescape(value)
        if changed == value:
            break
        value = changed
    parser = _Images()
    parser.feed(value)
    return [_reference(href, name, base_url=base_url, image=True)
            for href, name in parser.items
            if not only_file_routes or "/seeyon/fileUpload.do" in href or "/seeyon/fileDownload.do" in href]


def material_references(*, text="", attachments=(), base_url="", rendered_html=""):
    refs = image_references(text, base_url=base_url)
    refs += image_references(rendered_html, base_url=base_url, only_file_routes=True)
    for item in attachments:
        if isinstance(item, dict):
            refs.append(_reference(item.get("raw_href") or item.get("href") or "",
                                   item.get("name") or item.get("filename"), base_url=base_url))
    unique = {item["public"]["id"]: item for item in refs}
    return list(unique.values())[:MAX_REFERENCES]


def public_manifest(references):
    return [dict(item["public"]) for item in references]


def previews_for_review(review):
    """Pure projection also supports old cards retaining rich-text image refs."""
    if not isinstance(review, dict):
        return []
    def dictionaries(value):
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    refs = []
    supplied = review.get("materialPreviews")
    if isinstance(supplied, list):
        for item in supplied[:MAX_REFERENCES]:
            if (isinstance(item, dict) and _DIGEST.fullmatch(str(item.get("id") or ""))
                    and item.get("kind") in {"image", "pdf", "archive", "file"}
                    and item.get("state") in {"available", "unavailable"}):
                refs.append({key: item[key] for key in ("id", "name", "kind", "state", "reason") if key in item})
    # No URL is added to the projection, even when the frozen text contains it.
    values = []
    if not isinstance(supplied, list):
        values = [field.get("value") for field in dictionaries(review.get("fields"))]
        for section in dictionaries(review.get("sections")):
            values.append(section.get("text"))
            values.extend(field.get("value") for field in dictionaries(section.get("fields")))
        for group in dictionaries(review.get("groups")):
            values.extend(field.get("value") for field in dictionaries(group.get("fields")))
    for value in values:
        refs.extend(public_manifest(image_references(value)))
    known_names = {item.get("name") for item in refs if item.get("kind") != "image"}
    for item in dictionaries(review.get("attachments")):
        if item.get("name") not in known_names:
            refs.append(_reference("", item.get("name"))["public"])
    return list({item["id"]: item for item in refs}.values())[:MAX_REFERENCES]


def _file_type(body):
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if body.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    return None


def _archive(body, name):
    items, total, seen_names = [], 0, set()
    try:
        with ZipFile(BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ITEMS:
                _fail("MATERIAL_ARCHIVE_LIMIT", "附件包条目过多，请在 OA 中查看。")
            for entry in entries:
                parts = entry.filename.replace("\\", "/").split("/")
                if (entry.filename.startswith(("/", "\\")) or ".." in parts or "\x00" in entry.filename
                        or re.match(r"[A-Za-z]:", entry.filename) or entry.flag_bits & 1
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    _fail("MATERIAL_ARCHIVE_UNSAFE", "附件包包含无法安全预览的条目，请在 OA 中查看。")
                if entry.is_dir():
                    continue
                normalized_name = "/".join(part for part in parts if part != ".")
                if normalized_name in seen_names:
                    _fail("MATERIAL_ARCHIVE_AMBIGUOUS", "附件包存在同名条目，无法明确定位材料，请在 OA 中查看。")
                seen_names.add(normalized_name)
                total += entry.file_size
                if (entry.file_size > MAX_FILE_BYTES or total > MAX_ARCHIVE_EXPANDED
                        or entry.file_size > max(entry.compress_size, 1) * 100):
                    _fail("MATERIAL_ARCHIVE_LIMIT", "附件包展开后超过预览大小限制，请在 OA 中查看。")
                with archive.open(entry) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) != entry.file_size or len(data) > MAX_FILE_BYTES:
                    _fail("MATERIAL_ARCHIVE_INVALID", "附件包内容不完整，请重新获取或在 OA 中查看。")
                if (data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"Rar!", b"7z\xbc\xaf\x27\x1c"))
                        or entry.filename.lower().endswith((".zip", ".rar", ".7z", ".tar", ".gz"))):
                    # Office Open XML is also ZIP internally. It remains an
                    # unsupported document, never recursively unpacked.
                    nested = not entry.filename.lower().endswith((".docx", ".xlsx", ".pptx"))
                    if nested:
                        _fail("MATERIAL_ARCHIVE_NESTED", "附件包包含嵌套压缩文件，请在 OA 中查看。")
                detected = _file_type(data)
                item = {"id": sha256(entry.filename.encode()).hexdigest(), "name": entry.filename[:500],
                        "kind": detected[0] if detected else "file", "state": "available" if detected else "unavailable"}
                if detected:
                    item.update(contentType=detected[1], bodyBase64=base64.b64encode(data).decode(), byteLength=len(data))
                else:
                    item["reason"] = "此文件类型暂不支持预览，请在 OA 中查看。"
                items.append(item)
    except (BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        if isinstance(exc, MaterialPreviewError):
            raise
        _fail("MATERIAL_ARCHIVE_INVALID", "附件包无法完整读取，请在 OA 中查看。")
    return {"kind": "archive", "name": name, "items": items, "totalEntries": len(items)}


def fetch_current_material(adapter, worker, *, affair_id, expected):
    """Read one frozen reference only after proving current pending membership."""
    if not isinstance(affair_id, str) or not 1 <= len(affair_id) <= 256 or re.search(r"[\x00-\x1f]", affair_id):
        _fail("MATERIAL_SOURCE_UNSUPPORTED", "此办理入口暂不支持材料预览。", 404)
    pending = adapter._fetch_workflow_collection(worker, "pending")
    matches = [item for item in pending.get("items", []) if str(item.get("affair_id")) == affair_id]
    if len(matches) != 1:
        _fail("MATERIAL_SOURCE_CHANGED", "该事项已不在当前待办中，请重新查询。")
    source = matches[0]
    if source.get("category") == "会议":
        from agentbridge.adapters.seeyon_meeting_reply import meeting_target, _view, _snapshot
        target = meeting_target(adapter, source)
        snapshot = _snapshot(_view(adapter, worker, target["meeting_id"]), target["meeting_id"])
        if snapshot["meeting"]["title"] != source.get("title"):
            _fail("MATERIAL_SOURCE_CHANGED", "会议材料与当前待办不一致，请重新查询。")
        refs = material_references(text=snapshot["meeting"].get("content"),
            attachments=snapshot["meeting"].get("attachments", []), base_url=adapter.base_url)
    else:
        detail = adapter._render_workflow_source_detail(worker, source)
        refs = detail.get("_material_preview_sources", [])
    matches = [item for item in refs if item["public"] == expected and item["route"]]
    if len(matches) != 1:
        _fail("MATERIAL_SOURCE_CHANGED", "此材料已变化或缺少可核验的读取地址，请重新获取办理页面。")
    reference = matches[0]
    url = urljoin(adapter.base_url, reference["route"])
    limit = MAX_ARCHIVE_BYTES if expected["kind"] == "archive" else MAX_FILE_BYTES
    try:
        response = worker.request_bytes("GET", url, headers={"Accept": "image/png,image/jpeg,application/pdf,application/zip"},
            timeout_seconds=20, max_response_bytes=limit, deadline=monotonic() + 20)
    except Exception as exc:
        from agentbridge.core.capability_runtime import CapabilityRejected
        if isinstance(exc, CapabilityRejected):
            _fail("MATERIAL_READ_LIMIT", "材料读取超时、超过大小限制或响应不完整，请在 OA 中查看。", 413)
        if isinstance(exc, (TimeoutError, ConnectionError)):
            _fail("MATERIAL_READ_TIMEOUT", "材料读取超时或连接中断，尚未完成。请重新获取或在 OA 中查看。", 503)
        raise
    # Redirects (including a login redirect) are rejected, never followed.
    if response.get("status") != 200 or response.get("url") != url:
        _fail("MATERIAL_READ_DENIED", "当前登录无法读取此材料，请恢复登录或在 OA 中查看。", 403)
    body, content_type = response.get("body"), str(response.get("content_type") or "").split(";", 1)[0].strip().lower()
    if not isinstance(body, bytes) or not body:
        _fail("MATERIAL_EMPTY", "材料内容为空或读取失败，请重试。")
    if len(body) > limit:
        _fail("MATERIAL_TOO_LARGE", "材料超过预览大小限制，请在 OA 中查看。", 413)
    if expected["kind"] == "archive":
        if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"} or not body.startswith(b"PK"):
            _fail("MATERIAL_TYPE_MISMATCH", "附件包实际格式与读取合同不一致，请在 OA 中查看。")
        return {"contentType": "application/json", "archive": _archive(body, expected["name"])}
    detected = _file_type(body)
    if not detected or detected[0] != expected["kind"] or content_type not in {detected[1], "application/octet-stream"}:
        _fail("MATERIAL_TYPE_MISMATCH", "材料实际格式不支持安全预览，请在 OA 中查看。")
    return {"contentType": detected[1], "body": body}
