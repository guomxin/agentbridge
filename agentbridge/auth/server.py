from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import ipaddress
from pathlib import Path
import re
import ssl
from urllib.parse import urlparse

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import MAX_AUTH_BODY_BYTES, AuthCardResponse, TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.interactive_browser import TrustedInteractiveBrowserApplication
from agentbridge.auth.document_download import TrustedDocumentDownloadApplication
from agentbridge.auth.timeline_attachment import TrustedTimelineAttachmentApplication
from agentbridge.core.network_security import validate_insecure_private_http_endpoint
from agentbridge.core.tls_http import ThreadedTLSHTTPServer


@dataclass(frozen=True)
class AuthServerConfig:
    host: str
    port: int
    public_base_url: str
    tls_cert: Path | None
    tls_key: Path | None

    @property
    def secure_cookie(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def insecure_private_http(self) -> bool:
        return self.tls_cert is None and not _is_loopback_host(self.host)


class AuthHTTPServer(ThreadedTLSHTTPServer):
    pass


def validate_auth_server_config(
    *,
    host: str,
    port: int,
    public_base_url: str | None,
    tls_cert: str | Path | None,
    tls_key: str | Path | None,
    allow_insecure_private_http: bool = False,
) -> AuthServerConfig:
    if port < 0 or port > 65535:
        raise ValueError("authentication server port is invalid")
    cert = Path(tls_cert).resolve() if tls_cert else None
    key = Path(tls_key).resolve() if tls_key else None
    if (cert is None) != (key is None):
        raise ValueError("both TLS certificate and key are required")
    loopback = _is_loopback_host(host)
    if not loopback and cert is None and not allow_insecure_private_http:
        raise ValueError("non-loopback authentication card service requires TLS")
    if public_base_url is None:
        if not loopback:
            raise ValueError("non-loopback authentication card service requires public base URL")
        public_base_url = f"http://127.0.0.1:{port}"
    normalized_base_url = _normalize_public_base_url(public_base_url)
    if not loopback and cert is None:
        validate_insecure_private_http_endpoint(
            host=host,
            port=port,
            public_base_url=normalized_base_url,
            service_name="authentication card service",
        )
    elif not loopback and not normalized_base_url.startswith("https://"):
        raise ValueError("non-loopback authentication card public URL must use HTTPS")
    if cert is not None and not normalized_base_url.startswith("https://"):
        raise ValueError("TLS authentication card service must use an HTTPS public URL")
    return AuthServerConfig(
        host=host,
        port=port,
        public_base_url=normalized_base_url,
        tls_cert=cert,
        tls_key=key,
    )


def create_auth_http_server(
    *,
    config: AuthServerConfig,
    application: TrustedAuthApplication,
    action_application: TrustedActionApplication | None = None,
    field_application: TrustedFieldApplication | None = None,
    download_application: TrustedDocumentDownloadApplication | None = None,
    timeline_attachment_application: (
        TrustedTimelineAttachmentApplication | None
    ) = None,
    interactive_application: TrustedInteractiveBrowserApplication | None = None,
) -> ThreadingHTTPServer:
    expected_scheme = urlparse(config.public_base_url).scheme.lower()
    allowed_hosts = {_hostname(config.public_base_url)}
    if _is_loopback_host(config.host):
        allowed_hosts.update({"127.0.0.1", "localhost", "::1"})

    class AuthRequestHandler(BaseHTTPRequestHandler):
        server_version = "AgentBridgeAuth/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            return None

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._send(application._message_response(
                    status=400,
                    title="请求主机无效",
                    message="认证请求已被拒绝。",
                    tone="error",
                ))
                return
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            interactive_target = _interactive_browser_target(self.path)
            if interactive_target is not None and interactive_application is not None:
                challenge_id, action = interactive_target
                control_token = self.headers.get("X-AgentBridge-Control-Token") or ""
                if action == "status":
                    self._send(interactive_application.status(
                        challenge_id,
                        control_token=control_token,
                    ))
                    return
                self._send(application._message_response(
                    status=405,
                    title="请求方法不支持",
                    message="请返回安全登录卡片继续操作。",
                    tone="error",
                ))
                return
            document_file_id = _document_file_id_from_path(self.path)
            if document_file_id is not None and download_application is not None:
                self._send(download_application.get_file(document_file_id))
                return
            timeline_attachment_id = _timeline_attachment_id_from_path(self.path)
            if (
                timeline_attachment_id is not None
                and timeline_attachment_application is not None
            ):
                self._send(
                    timeline_attachment_application.get_file(
                        timeline_attachment_id
                    )
                )
                return
            card_application, card_id, presentation_id = self._card_target()
            if card_application is None or card_id is None:
                self._send(application._message_response(
                    status=404,
                    title="页面不存在",
                    message="请从智能体打开可信卡片。",
                    tone="error",
                ))
                return
            card_arguments = {"secure_cookie": config.secure_cookie}
            if card_application in {action_application, field_application}:
                card_arguments["presentation_id"] = presentation_id
            self._send(card_application.get_card(card_id, **card_arguments))

        def do_POST(self) -> None:
            if not self._host_allowed() or not self._origin_allowed():
                self._send(application._message_response(
                    status=403,
                    title="请求来源无效",
                    message="认证请求已被拒绝。",
                    tone="error",
                ))
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_length = -1
            if content_length < 0 or content_length > MAX_AUTH_BODY_BYTES:
                self.close_connection = True
                self._send(application._message_response(
                    status=413,
                    title="请求过大",
                    message="认证请求已被拒绝。",
                    tone="error",
                ))
                return
            body = self.rfile.read(content_length)
            csrf_cookie = _csrf_cookie(self.headers.get("Cookie") or "")
            interactive_target = _interactive_browser_target(self.path)
            if interactive_target is not None and interactive_application is not None:
                challenge_id, action = interactive_target
                try:
                    if action == "start":
                        response = interactive_application.start(
                            challenge_id,
                            body=body,
                            content_type=self.headers.get("Content-Type") or "",
                            csrf_cookie=csrf_cookie,
                        )
                    else:
                        response = application._message_response(
                            status=405,
                            title="请求方法不支持",
                            message="请返回安全登录卡片继续操作。",
                            tone="error",
                        )
                finally:
                    body = b""
                self._send(response)
                return
            card_application, card_id, presentation_id = self._card_target()
            if card_application is None or card_id is None:
                self._send(application._message_response(
                    status=404,
                    title="页面不存在",
                    message="请从智能体打开可信卡片。",
                    tone="error",
                ))
                return
            try:
                card_arguments = {
                    "body": body,
                    "content_type": self.headers.get("Content-Type") or "",
                    "csrf_cookie": csrf_cookie,
                }
                if card_application in {action_application, field_application}:
                    card_arguments["presentation_id"] = presentation_id
                response = card_application.submit_card(
                    card_id,
                    **card_arguments,
                )
            finally:
                body = b""
            self._send(response)

        def do_OPTIONS(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _host_allowed(self) -> bool:
            try:
                host = _host_header_name(self.headers.get("Host") or "")
            except ValueError:
                return False
            return host in allowed_hosts

        def _origin_allowed(self) -> bool:
            return _request_origin_allowed(
                origin=self.headers.get("Origin"),
                sec_fetch_site=self.headers.get("Sec-Fetch-Site"),
                host_header=self.headers.get("Host") or "",
                expected_scheme=expected_scheme,
                allowed_hosts=allowed_hosts,
                allow_opaque_without_fetch_metadata=config.insecure_private_http,
            )

        def _card_target(self):
            challenge_id = _challenge_id_from_path(self.path)
            if challenge_id is not None:
                if interactive_application is not None:
                    try:
                        challenge = application.challenge_store.get(challenge_id)
                    except Exception:
                        challenge = None
                    if (
                        challenge is not None
                        and challenge.get("challenge_type")
                        == "interactive_browser_login"
                    ):
                        return interactive_application, challenge_id, None
                return application, challenge_id, None
            authorization_target = _authorization_target_from_path(self.path)
            if authorization_target is not None and action_application is not None:
                authorization_id, presentation_id = authorization_target
                return action_application, authorization_id, presentation_id
            submission_target = _field_submission_target_from_path(self.path)
            if submission_target is not None and field_application is not None:
                submission_id, presentation_id = submission_target
                return field_application, submission_id, presentation_id
            download_id = _document_download_id_from_path(self.path)
            if download_id is not None and download_application is not None:
                return download_application, download_id, None
            return None, None, None

        def _send(self, response: AuthCardResponse) -> None:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            if config.secure_cookie:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

    server = AuthHTTPServer((config.host, config.port), AuthRequestHandler)
    if config.tls_cert is not None and config.tls_key is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.enable_tls(context)
    return server


def serve_auth_cards(
    *,
    config: AuthServerConfig,
    application: TrustedAuthApplication,
    action_application: TrustedActionApplication | None = None,
    field_application: TrustedFieldApplication | None = None,
    download_application: TrustedDocumentDownloadApplication | None = None,
    timeline_attachment_application: (
        TrustedTimelineAttachmentApplication | None
    ) = None,
    interactive_application: TrustedInteractiveBrowserApplication | None = None,
) -> None:
    server = create_auth_http_server(
        config=config,
        application=application,
        action_application=action_application,
        field_application=field_application,
        download_application=download_application,
        timeline_attachment_application=timeline_attachment_application,
        interactive_application=interactive_application,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def _interactive_browser_target(path: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"/auth/([A-Za-z0-9_-]{32,128})/interactive/(start|status)",
        path.split("?", 1)[0],
    )
    return (match.group(1), match.group(2)) if match else None


def _challenge_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"/auth/([A-Za-z0-9_-]{32,128})", path.split("?", 1)[0])
    return match.group(1) if match else None


def _authorization_target_from_path(
    path: str,
) -> tuple[str, str | None] | None:
    normalized = path.split("?", 1)[0]
    presentation = re.fullmatch(
        r"/authorize/([A-Za-z0-9_-]{32,128})/present/"
        r"([A-Za-z0-9_-]{32,128})",
        normalized,
    )
    if presentation:
        return presentation.group(1), presentation.group(2)
    legacy = re.fullmatch(
        r"/authorize/([A-Za-z0-9_-]{32,128})",
        normalized,
    )
    if legacy:
        return legacy.group(1), None
    return None


def _authorization_id_from_path(path: str) -> str | None:
    target = _authorization_target_from_path(path)
    return target[0] if target is not None else None


def _field_submission_target_from_path(
    path: str,
) -> tuple[str, str | None] | None:
    normalized = path.split("?", 1)[0]
    presentation = re.fullmatch(
        r"/input/([A-Za-z0-9_-]{32,128})/present/"
        r"([A-Za-z0-9_-]{32,128})",
        normalized,
    )
    if presentation:
        return presentation.group(1), presentation.group(2)
    legacy = re.fullmatch(
        r"/input/([A-Za-z0-9_-]{32,128})",
        normalized,
    )
    if legacy:
        return legacy.group(1), None
    return None


def _field_submission_id_from_path(path: str) -> str | None:
    target = _field_submission_target_from_path(path)
    return target[0] if target is not None else None


def _document_download_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"/download/([A-Za-z0-9_-]{32,128})", path.split("?", 1)[0])
    return match.group(1) if match else None

def _document_file_id_from_path(path: str) -> str | None:
    match = re.fullmatch(
        r"/download/([A-Za-z0-9_-]{32,128})/file",
        path.split("?", 1)[0],
    )
    return match.group(1) if match else None


def _timeline_attachment_id_from_path(path: str) -> str | None:
    match = re.fullmatch(
        r"/media/([A-Za-z0-9_-]{32,128})/file",
        path.split("?", 1)[0],
    )
    return match.group(1) if match else None


def _csrf_cookie(value: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(value)
    except Exception:
        return ""
    morsel = cookie.get("agentbridge_csrf")
    return morsel.value if morsel is not None else ""


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_public_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("authentication card public base URL must be http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("authentication card public base URL is invalid")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _origin(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _hostname(value: str) -> str:
    hostname = urlparse(value).hostname
    if not hostname:
        raise ValueError("public base URL hostname is required")
    return hostname.lower()


def _host_header_name(value: str) -> str:
    parsed = urlparse(f"//{value}")
    if not parsed.hostname:
        raise ValueError("Host header is invalid")
    return parsed.hostname.lower()


def _request_origin_allowed(
    *,
    origin: str | None,
    sec_fetch_site: str | None,
    host_header: str,
    expected_scheme: str,
    allowed_hosts: set[str],
    allow_opaque_without_fetch_metadata: bool = False,
) -> bool:
    # Some browsers serialize a same-origin form POST from a loopback or
    # private-HTTP page as an opaque origin. Fetch Metadata is preferred proof;
    # the explicit private-HTTP PoC mode falls back to the card's CSRF binding.
    if origin is None:
        return True
    if origin.strip().lower() == "null":
        fetch_site = (sec_fetch_site or "").strip().lower()
        return fetch_site == "same-origin" or (
            allow_opaque_without_fetch_metadata and not fetch_site
        )

    try:
        parsed_origin = urlparse(origin)
        parsed_host = urlparse(f"//{host_header}")
        origin_host = (parsed_origin.hostname or "").lower()
        request_host = (parsed_host.hostname or "").lower()
        origin_port = parsed_origin.port or _default_port(parsed_origin.scheme)
        request_port = parsed_host.port or _default_port(expected_scheme)
    except ValueError:
        return False

    return (
        parsed_origin.scheme.lower() == expected_scheme
        and parsed_origin.username is None
        and parsed_origin.password is None
        and parsed_origin.path in {"", "/"}
        and not parsed_origin.params
        and not parsed_origin.query
        and not parsed_origin.fragment
        and origin_host in allowed_hosts
        and request_host in allowed_hosts
        and origin_port == request_port
    )


def _default_port(scheme: str) -> int | None:
    if scheme.lower() == "http":
        return 80
    if scheme.lower() == "https":
        return 443
    return None
