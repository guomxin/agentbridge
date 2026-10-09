from __future__ import annotations

from html import escape
import logging
import re
import secrets
from typing import Callable
from urllib.parse import parse_qs, quote

from agentbridge.auth.card import MAX_AUTH_BODY_BYTES, AuthCardResponse
from agentbridge.core.document_downloads import (
    DocumentDownloadAccessDenied,
    DocumentDownloadIntegrityError,
    DocumentDownloadNotFound,
    DocumentDownloadStateError,
    DocumentDownloadStore,
    SUPPORTED_DOCUMENT_CONTENT_TYPES,
)


_LOGGER = logging.getLogger("uvicorn.error")
_CONTENT_TYPE_SUFFIXES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/csv": ".csv",
}


class TrustedDocumentDownloadApplication:
    def __init__(
        self,
        *,
        download_store: DocumentDownloadStore,
        fetcher: Callable[[dict], dict],
    ) -> None:
        self.download_store = download_store
        self.fetcher = fetcher

    def get_card(self, download_id: str, *, secure_cookie: bool) -> AuthCardResponse:
        try:
            record = self.download_store.get(download_id, include_document=True)
            self.download_store.require_access(record)
        except PermissionError:
            return _message_response(
                status=403, title="当前无权下载该文件",
                message="请联系管理员核对该文件及其来源的访问权限。", tone="error",
            )
        except DocumentDownloadNotFound:
            return _message_response(
                status=404,
                title="下载链接不存在",
                message="请返回智能体重新生成文件。",
                tone="error",
            )
        except DocumentDownloadIntegrityError:
            return _message_response(
                status=409,
                title="下载链接不可用",
                message="下载引用校验失败，请返回智能体重新生成文件。",
                tone="error",
            )
        if record["state"] == "ready":
            return self.get_file(download_id)
        if record["state"] == "pending":
            csrf_token = self.download_store.issue_csrf(download_id)
            nonce = secrets.token_urlsafe(18)
            headers = _security_headers(nonce)
            cookie = (
                f"agentbridge_csrf={csrf_token}; Path=/download/{download_id}; "
                "HttpOnly; SameSite=Strict"
            )
            if secure_cookie:
                cookie += "; Secure"
            headers["Set-Cookie"] = cookie
            return AuthCardResponse(
                200,
                headers,
                _render_download_form(record, csrf_token=csrf_token, nonce=nonce).encode(
                    "utf-8"
                ),
            )
        messages = {
            "processing": ("下载处理中", "AgentBridge 正在准备文件，请稍候。"),
            "completed": ("链接已使用", "请返回智能体生成新的下载链接。"),
            "expired": ("下载链接已失效", "请返回智能体重新生成文件。"),
        }
        title, message = messages.get(
            record["state"],
            ("下载链接不可用", "请返回智能体重新生成文件。"),
        )
        return _message_response(
            status=409,
            title=title,
            message=message,
            tone="neutral",
        )

    def get_file(self, download_id: str) -> AuthCardResponse:
        try:
            payload = self.download_store.ready_payload(download_id)
        except DocumentDownloadNotFound:
            return _message_response(
                status=404,
                title="下载链接不存在",
                message="请返回智能体重新生成文件。",
                tone="error",
            )
        except PermissionError:
            return _message_response(
                status=403,
                title="当前无权下载该文件",
                message="请联系管理员核对该文件及其来源的访问权限。",
                tone="error",
            )
        except (
            DocumentDownloadIntegrityError,
            DocumentDownloadStateError,
        ):
            return _message_response(
                status=409,
                title="文件尚未准备好",
                message="请返回智能体重新准备或下载文件。",
                tone="error",
            )
        content_type = str(payload["content_type"])
        filename = str(payload["filename"])
        ascii_name = _ascii_download_name(payload["document_type"], content_type)
        disposition = (
            f"attachment; filename={ascii_name}; "
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        return AuthCardResponse(
            200,
            {
                "Cache-Control": "private, no-store",
                "Content-Disposition": disposition,
                "Content-Type": content_type,
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
            payload["body"],
        )

    def submit_card(
        self,
        download_id: str,
        *,
        body: bytes,
        content_type: str,
        csrf_cookie: str,
    ) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return _message_response(
                status=413,
                title="请求过大",
                message="请刷新下载页面后重试。",
                tone="error",
            )
        if content_type.split(";", 1)[0].strip().lower() != (
            "application/x-www-form-urlencoded"
        ):
            return _message_response(
                status=415,
                title="请求格式无效",
                message="请刷新下载页面后重试。",
                tone="error",
            )
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            csrf_token = (values.get("csrf_token") or [""])[-1]
        except (UnicodeDecodeError, ValueError):
            csrf_token = ""
        try:
            record = self.download_store.claim(
                download_id,
                csrf_token=csrf_token,
                csrf_cookie=csrf_cookie,
            )
        except DocumentDownloadNotFound:
            return _message_response(
                status=404,
                title="下载链接不存在",
                message="请返回智能体重新生成文件。",
                tone="error",
            )
        except (
            DocumentDownloadAccessDenied,
            DocumentDownloadIntegrityError,
            DocumentDownloadStateError,
        ):
            return _message_response(
                status=409,
                title="下载请求已失效",
                message="请刷新页面；若仍失败，请返回智能体重新生成文件。",
                tone="error",
            )

        try:
            self.download_store.require_access(record)
            payload = self.fetcher(record)
            self.download_store.require_access(record)
            file_body = payload["body"]
            filename = str(payload.get("filename") or record["filename"])
            content_type = str(payload.get("content_type") or "application/pdf")
            if not isinstance(file_body, bytes):
                raise TypeError("document fetcher did not return bytes")
            if content_type not in SUPPORTED_DOCUMENT_CONTENT_TYPES:
                raise TypeError("document fetcher returned an unsupported content type")
            self.download_store.complete(download_id)
        except PermissionError:
            self.download_store.release(download_id)
            return _message_response(
                status=403, title="当前无权下载该文件",
                message="请联系管理员核对该文件及其来源的访问权限。", tone="error",
            )
        except Exception as exc:
            error_code = _safe_error_code(exc)
            _LOGGER.warning(
                "AgentBridge document download failed: error=%s detail=%s",
                error_code,
                _safe_error_detail(exc),
            )
            try:
                self.download_store.release(download_id)
            except DocumentDownloadStateError:
                pass
            return _message_response(
                status=502,
                title="暂时无法下载",
                message=(
                    "AgentBridge 未能从目标系统取得文件，请刷新页面重试。"
                    f"错误代码：{error_code}。"
                ),
                tone="error",
            )

        ascii_name = _ascii_download_name(record["document_type"], content_type)
        disposition = (
            f"attachment; filename={ascii_name}; "
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        return AuthCardResponse(
            200,
            {
                "Cache-Control": "no-store",
                "Content-Disposition": disposition,
                "Content-Type": content_type,
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
            file_body,
        )


def _render_download_form(record: dict, *, csrf_token: str, nonce: str) -> str:
    type_label = {
        "patent_certificate": "专利证书",
        "software_copyright_certificate": "软件著作权证书",
        "smartlight_csv_report": "照明分析报告",
        "oa_addressbook_csv_report": "OA 通讯录",
    }.get(record["document_type"], "文件")
    action_label = (
        "下载证书扫描件"
        if record["document_type"] in {
            "patent_certificate",
            "software_copyright_certificate",
        }
        else "下载报告"
    )
    size_html = (
        f'<p class="meta">文件大小：{escape(record["display_size"])}</p>'
        if record.get("display_size")
        else ""
    )
    return _document(
        title=f"下载{type_label}",
        nonce=nonce,
        body=f"""
        <main class="shell">
          <section class="card" aria-labelledby="download-title">
            <div class="mark" aria-hidden="true"></div>
            <p class="eyebrow">AGENTBRIDGE TRUSTED DOWNLOAD</p>
            <h1 id="download-title">下载{escape(type_label)}</h1>
            <p class="filename">{escape(record["filename"])}</p>
            {size_html}
            <form method="post" action="/download/{escape(record["download_id"])}">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              <button type="submit">{escape(action_label)}</button>
            </form>
            <p class="hint">链接为一次性短时授权，文件不会经过聊天内容。</p>
          </section>
        </main>
        """,
    )


def _ascii_download_name(document_type: str, content_type: str) -> str:
    suffix = _CONTENT_TYPE_SUFFIXES[content_type]
    stem = (
        "certificate"
        if document_type in {
            "patent_certificate",
            "software_copyright_certificate",
        }
        else "agentbridge-report"
    )
    return f"{stem}{suffix}"


def _message_response(
    *,
    status: int,
    title: str,
    message: str,
    tone: str,
) -> AuthCardResponse:
    nonce = secrets.token_urlsafe(18)
    body = _document(
        title=title,
        nonce=nonce,
        body=f"""
        <main class="shell">
          <section class="card status {escape(tone)}">
            <div class="mark" aria-hidden="true"></div>
            <p class="eyebrow">AGENTBRIDGE TRUSTED DOWNLOAD</p>
            <h1>{escape(title)}</h1>
            <p class="filename">{escape(message)}</p>
          </section>
        </main>
        """,
    )
    return AuthCardResponse(status, _security_headers(nonce), body.encode("utf-8"))


def _security_headers(nonce: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; "
            f"script-src 'nonce-{nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _document(*, title: str, nonce: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style nonce="{nonce}">
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      padding: 24px; background: #f1f4f2; color: #17211d;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }}
    .shell {{ width: min(620px, 100%); }}
    .card {{
      background: #fff; border: 1px solid #cfd8d3; border-radius: 8px;
      padding: 34px; box-shadow: 0 20px 48px rgba(27, 48, 39, .12);
    }}
    .mark {{ width: 22px; height: 22px; border: 5px solid #bf4141;
      transform: rotate(45deg); margin: 0 auto 18px; }}
    .eyebrow {{ margin: 0 0 8px; color: #006b5f; text-align: center;
      font-size: 12px; font-weight: 800; }}
    h1 {{ margin: 0 0 20px; text-align: center; font-size: 25px; }}
    .filename {{ margin: 0 0 12px; line-height: 1.65; overflow-wrap: anywhere; }}
    .meta, .hint {{ color: #52635c; font-size: 14px; line-height: 1.6; }}
    button {{
      width: 100%; min-height: 48px; margin-top: 18px; border: 0;
      border-radius: 6px; background: #006b5f; color: #fff;
      font-size: 16px; font-weight: 700; cursor: pointer;
    }}
  </style>
</head>
<body>{body}</body>
</html>"""


def _safe_error_code(exc: Exception) -> str:
    value = re.sub(r"[^A-Z0-9_.-]", "_", exc.__class__.__name__.upper())[:80]
    return value or "DOCUMENT_DOWNLOAD_FAILED"

def _safe_error_detail(exc: Exception) -> str:
    if exc.__class__.__name__ not in {
        "SeeyonDocumentAccessDenied",
        "SeeyonDocumentContractMismatch",
        "AdapterLoginRequired",
    }:
        return "redacted"
    return re.sub(r"[\r\n\t]+", " ", str(exc))[:240]
