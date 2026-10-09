from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import string
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlencode, urlparse

from agentbridge.adapters.base import AdapterLoginRequired, AdapterSessionCheckUnavailable
from agentbridge.core.auth_challenges import AuthChallengeStore
from agentbridge.core.session_secrets import SessionSecretError, SessionStateStore
from agentbridge.core.sessions import SessionPrincipalMismatch, SessionRegistry


logger = logging.getLogger(__name__)

_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_RUNTIME_MARKER = ".agentbridge-remote-browser-root"


class RemoteBrowserAccessDenied(RuntimeError):
    pass


class RemoteBrowserUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteBrowserConfig:
    runtime_root: Path
    public_base_url: str
    listen_host: str
    listen_port: int
    tls_cert: Path | None
    tls_key: Path | None
    chrome_executable: Path | None = None
    novnc_web_root: Path = Path("/usr/share/novnc")
    display_start: int = 100
    slot_count: int = 32
    rfb_port_start: int = 5901
    cdp_port_start: int = 9222
    allow_insecure_private_http: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))
        object.__setattr__(
            self,
            "chrome_executable",
            Path(self.chrome_executable) if self.chrome_executable else None,
        )
        object.__setattr__(
            self,
            "tls_cert",
            Path(self.tls_cert) if self.tls_cert else None,
        )
        object.__setattr__(
            self,
            "tls_key",
            Path(self.tls_key) if self.tls_key else None,
        )
        object.__setattr__(self, "novnc_web_root", Path(self.novnc_web_root))
        if not self.runtime_root.is_absolute():
            raise ValueError("remote browser runtime root must be absolute")
        if self.listen_host in {"0.0.0.0", "::", ""}:
            raise ValueError("remote browser listen host must be explicit")
        address = ipaddress.ip_address(self.listen_host)
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError("remote browser listen host is not allowed")
        parsed = urlparse(self.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("remote browser public URL must use HTTP(S)")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("remote browser public URL must not include a path")
        if parsed.hostname != self.listen_host:
            raise ValueError("remote browser public URL must match the listen host")
        if parsed.port != self.listen_port:
            raise ValueError("remote browser public URL must match the listen port")
        if parsed.scheme == "https":
            if self.tls_cert is None or self.tls_key is None:
                raise ValueError("HTTPS remote browser requires a certificate and key")
        elif not address.is_loopback and not self.allow_insecure_private_http:
            raise ValueError("non-loopback remote browser access must use HTTPS")
        if not 1024 <= self.listen_port <= 65535:
            raise ValueError("remote browser listen port is outside the allowed range")
        if not 1 <= self.display_start <= 999:
            raise ValueError("remote browser display range is invalid")
        if not 1 <= self.slot_count <= 100:
            raise ValueError("remote browser slot count is invalid")
        for start in (self.rfb_port_start, self.cdp_port_start):
            if not 1024 <= start <= 65535:
                raise ValueError("remote browser port range is invalid")
            if start + self.slot_count - 1 > 65535:
                raise ValueError("remote browser port range exceeds 65535")


@dataclass(frozen=True)
class _RemoteBrowserAllocation:
    slot: int
    display: int
    rfb_port: int
    cdp_port: int


class RemoteInteractiveBrowserBroker:
    """Own isolated, short-lived noVNC login sessions for trusted cards."""

    def __init__(
        self,
        *,
        challenge_store: AuthChallengeStore,
        session_registry: SessionRegistry,
        session_state_store: SessionStateStore,
        adapter_factory: Callable[[dict], object],
        worker_factory: Callable[[dict, object, str], AbstractContextManager],
        config: RemoteBrowserConfig,
        login_timeout_seconds: float = 900,
    ) -> None:
        if login_timeout_seconds < 30 or login_timeout_seconds > 900:
            raise ValueError("interactive login timeout must be between 30 and 900 seconds")
        self.challenge_store = challenge_store
        self.session_registry = session_registry
        self.session_state_store = session_state_store
        self.adapter_factory = adapter_factory
        self.worker_factory = worker_factory
        self.config = config
        self.login_timeout_seconds = login_timeout_seconds
        self._lock = threading.Lock()
        self._runs: dict[str, _RemoteInteractiveRun] = {}
        self._allocated_slots: set[int] = set()
        self._gateway = _NoVncGateway(config)
        self._prepare_runtime_root()

    @property
    def public_origin(self) -> str:
        parsed = urlparse(self.config.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def start(
        self,
        *,
        challenge_id: str,
        csrf_token: str,
        csrf_cookie: str,
    ) -> dict:
        challenge = self.challenge_store.claim(
            challenge_id,
            csrf_token=csrf_token,
            csrf_cookie=csrf_cookie,
        )
        session = self.session_registry.get(challenge["session_id"])
        adapter = self.adapter_factory(challenge)
        contract = adapter.authentication_contract()
        allocation = None
        run_started = False
        try:
            self._validate_bindings(challenge, session, contract)
            self._gateway.ensure_started()
            allocation = self._allocate()
            self.session_state_store.delete(session["session_id"])
            self.session_registry.mark_awaiting_login(
                session["session_id"],
                source="remote_browser_challenge",
            )
            run = _RemoteInteractiveRun(
                challenge=challenge,
                session=session,
                contract=contract,
                adapter=adapter,
                worker_factory=self.worker_factory,
                challenge_store=self.challenge_store,
                session_registry=self.session_registry,
                session_state_store=self.session_state_store,
                config=self.config,
                allocation=allocation,
                token_directory=self._gateway.token_directory,
                gateway_alive=self._gateway.require_alive,
                timeout_seconds=min(
                    self.login_timeout_seconds,
                    _remaining_challenge_seconds(challenge),
                ),
                on_finished=lambda: self._release(challenge_id, allocation.slot),
            )
            with self._lock:
                existing = self._runs.get(challenge_id)
                if existing is not None and not existing.done:
                    self._allocated_slots.discard(allocation.slot)
                    raise RuntimeError("interactive browser login is already running")
                self._runs[challenge_id] = run
            run.start()
            run_started = True
            if not run.wait_until_ready(timeout_seconds=25):
                if run.done:
                    return self.status(challenge_id=challenge_id)
                run.stop()
                return self._fail(
                    challenge_id,
                    session,
                    code="REMOTE_BROWSER_START_TIMEOUT",
                    message="The trusted remote browser did not start in time.",
                )
            return {
                "status": "processing",
                "challengeId": challenge_id,
                "controlToken": run.control_token,
                "remoteUrl": run.remote_url,
                "expiresAt": challenge["expires_at"],
            }
        except Exception:
            if allocation is not None and not run_started:
                self._release(challenge_id, allocation.slot)
            logger.exception("Remote browser start failed for challenge %s", challenge_id)
            return self._fail(
                challenge_id,
                session,
                code="REMOTE_BROWSER_START_FAILED",
                message="The trusted remote browser could not be started.",
            )

    def status(
        self,
        *,
        challenge_id: str,
        control_token: str | None = None,
    ) -> dict:
        if control_token is not None:
            run = self._authorized_run(challenge_id, control_token, allow_done=True)
            verification = run.verification_status
        else:
            verification = None
        challenge = self.challenge_store.get(challenge_id)
        response = {
            "status": challenge["state"],
            "challengeId": challenge_id,
            "error": challenge.get("error"),
            "result": challenge.get("result"),
        }
        if verification:
            response["verification"] = verification
        return response

    def shutdown(self) -> None:
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.stop()
        for run in runs:
            run.join(timeout_seconds=8)
        self._gateway.stop()

    def _prepare_runtime_root(self) -> None:
        root = self.config.runtime_root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        marker = root / _RUNTIME_MARKER
        entries = list(root.iterdir())
        if entries and not marker.is_file():
            raise ValueError("remote browser runtime root is not AgentBridge-managed")
        marker.write_text("agentbridge remote browser runtime\n", encoding="ascii")
        marker.chmod(0o600)
        sessions = root / "sessions"
        tokens = root / "tokens"
        if sessions.exists():
            shutil.rmtree(sessions)
        if tokens.exists():
            shutil.rmtree(tokens)
        sessions.mkdir(mode=0o700)
        tokens.mkdir(mode=0o700)

    def _allocate(self) -> _RemoteBrowserAllocation:
        with self._lock:
            for slot in range(self.config.slot_count):
                if slot in self._allocated_slots:
                    continue
                display = self.config.display_start + slot
                rfb_port = self.config.rfb_port_start + slot
                cdp_port = self.config.cdp_port_start + slot
                if not _port_available(rfb_port) or not _port_available(cdp_port):
                    continue
                if Path(f"/tmp/.X11-unix/X{display}").exists():
                    continue
                self._allocated_slots.add(slot)
                return _RemoteBrowserAllocation(
                    slot=slot,
                    display=display,
                    rfb_port=rfb_port,
                    cdp_port=cdp_port,
                )
        raise RemoteBrowserUnavailable("no isolated remote browser slot is available")

    def _release(self, challenge_id: str, slot: int) -> None:
        del challenge_id
        with self._lock:
            self._allocated_slots.discard(slot)

    def _authorized_run(
        self,
        challenge_id: str,
        control_token: str,
        *,
        allow_done: bool = False,
    ) -> _RemoteInteractiveRun:
        with self._lock:
            run = self._runs.get(challenge_id)
        if run is None or (run.done and not allow_done):
            raise RemoteBrowserUnavailable("interactive browser login is not available")
        if not hmac.compare_digest(run.control_token, str(control_token or "")):
            raise RemoteBrowserAccessDenied("interactive browser control token is invalid")
        return run

    def _fail(
        self,
        challenge_id: str,
        session: dict,
        *,
        code: str,
        message: str,
    ) -> dict:
        try:
            self.session_registry.mark_expired(
                session["session_id"],
                message,
                source="remote_browser_login",
            )
        except Exception:
            pass
        try:
            self.session_state_store.delete(session["session_id"])
        except SessionSecretError:
            pass
        try:
            challenge = self.challenge_store.fail(
                challenge_id,
                code=code,
                message=message,
            )
        except Exception:
            challenge = self.challenge_store.get(challenge_id)
        return {
            "status": "failed",
            "challengeId": challenge_id,
            "error": challenge.get("error") or {"code": code, "message": message},
        }

    @staticmethod
    def _validate_bindings(challenge: dict, session: dict, contract: dict) -> None:
        if challenge["challenge_type"] != "interactive_browser_login":
            raise ValueError("challenge is not an interactive browser login")
        if contract.get("authentication_mode") != "interactive_browser":
            raise ValueError("adapter does not declare interactive browser login")
        if any(
            (
                session["session_id"] != challenge["session_id"],
                session["user_subject"] != challenge["user_subject"],
                session["system_id"] != challenge["system_id"],
                contract.get("system_id") != challenge["system_id"],
                contract.get("origin") != challenge["origin"],
                contract.get("page_fingerprint") != challenge["page_fingerprint"],
                contract.get("fields") != challenge["fields"],
            )
        ):
            raise ValueError("interactive authentication binding mismatch")


class _NoVncGateway:
    def __init__(self, config: RemoteBrowserConfig) -> None:
        self.config = config
        self.token_directory = config.runtime_root / "tokens"
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    def ensure_started(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            _require_command("websockify")
            if not self.config.novnc_web_root.is_dir():
                raise RuntimeError("noVNC web root is unavailable")
            command = [
                "websockify",
                f"--web={self.config.novnc_web_root}",
                "--heartbeat=30",
                "--token-plugin=TokenFile",
                f"--token-source={self.token_directory}",
            ]
            if self.config.tls_cert is not None and self.config.tls_key is not None:
                command.extend(
                    [
                        f"--cert={self.config.tls_cert}",
                        f"--key={self.config.tls_key}",
                        "--ssl-only",
                    ]
                )
            command.append(f"{self.config.listen_host}:{self.config.listen_port}")
            process = _start_process(command, os.environ.copy())
            self._process = process
        try:
            _wait_until(
                lambda: _port_ready(self.config.listen_host, self.config.listen_port),
                [process],
                "noVNC gateway did not become ready",
            )
        except Exception:
            with self._lock:
                if self._process is process:
                    self._process = None
            _terminate_process(process)
            raise

    def require_alive(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            raise RemoteBrowserUnavailable("noVNC gateway is unavailable")

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            _terminate_process(process)


class _RemoteInteractiveRun:
    def __init__(
        self,
        *,
        challenge: dict,
        session: dict,
        contract: dict,
        adapter: object,
        worker_factory: Callable[[dict, object, str], AbstractContextManager],
        challenge_store: AuthChallengeStore,
        session_registry: SessionRegistry,
        session_state_store: SessionStateStore,
        config: RemoteBrowserConfig,
        allocation: _RemoteBrowserAllocation,
        token_directory: Path,
        gateway_alive: Callable[[], None],
        timeout_seconds: float,
        on_finished: Callable[[], None],
    ) -> None:
        self.challenge = challenge
        self.session = session
        self.contract = contract
        self.adapter = adapter
        self.worker_factory = worker_factory
        self.challenge_store = challenge_store
        self.session_registry = session_registry
        self.session_state_store = session_state_store
        self.config = config
        self.allocation = allocation
        self.token_directory = token_directory
        self.gateway_alive = gateway_alive
        self.timeout_seconds = timeout_seconds
        self.on_finished = on_finished
        self.control_token = secrets.token_urlsafe(32)
        self.route_token = secrets.token_urlsafe(32)
        self.vnc_password = "".join(
            secrets.choice(_PASSWORD_ALPHABET) for _ in range(8)
        )
        self.remote_url = _remote_url(
            self.config.public_base_url,
            route_token=self.route_token,
            password=self.vnc_password,
        )
        self._ready = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._verification_lock = threading.Lock()
        self._verification_status = "starting"
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentbridge-remote-login-{challenge['challenge_id'][:10]}",
            daemon=True,
        )

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def verification_status(self) -> str:
        with self._verification_lock:
            return self._verification_status

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, *, timeout_seconds: float) -> None:
        self._thread.join(timeout=max(timeout_seconds, 0))

    def wait_until_ready(self, *, timeout_seconds: float) -> bool:
        return self._ready.wait(timeout=max(timeout_seconds, 0))

    def _set_verification_status(self, value: str) -> None:
        with self._verification_lock:
            self._verification_status = value

    def _run(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        runtime_dir = (
            self.config.runtime_root
            / "sessions"
            / self.challenge["challenge_id"]
        )
        route_path = self.token_directory / f"{self.route_token}.route"
        processes: list[subprocess.Popen] = []
        try:
            runtime_dir.mkdir(mode=0o700)
            profile_dir = runtime_dir / "profile"
            profile_dir.mkdir(mode=0o700)
            authority_path = runtime_dir / "Xauthority"
            password_path = runtime_dir / "vnc-password"
            password_path.write_text(self.vnc_password + "\n", encoding="ascii")
            password_path.chmod(0o600)
            _create_xauthority(authority_path, self.allocation.display)
            environment = dict(os.environ)
            environment.update(
                {
                    "DISPLAY": f":{self.allocation.display}",
                    "XAUTHORITY": str(authority_path),
                    "HOME": str(runtime_dir),
                }
            )
            processes.append(
                _start_process(
                    [
                        "Xvfb",
                        f":{self.allocation.display}",
                        "-screen",
                        "0",
                        "900x1100x24",
                        "-nolisten",
                        "tcp",
                        "-noreset",
                        "-auth",
                        str(authority_path),
                    ],
                    environment,
                )
            )
            _wait_until(
                lambda: Path(
                    f"/tmp/.X11-unix/X{self.allocation.display}"
                ).exists(),
                processes,
                "isolated Xvfb display did not become ready",
            )
            processes.append(
                _start_process(
                    [
                        "x11vnc",
                        "-display",
                        f":{self.allocation.display}",
                        "-auth",
                        str(authority_path),
                        "-rfbport",
                        str(self.allocation.rfb_port),
                        "-localhost",
                        "-forever",
                        "-shared",
                        "-passwdfile",
                        str(password_path),
                        "-noxdamage",
                        "-quiet",
                    ],
                    environment,
                )
            )
            _wait_until(
                lambda: _port_ready("127.0.0.1", self.allocation.rfb_port),
                processes,
                "isolated x11vnc server did not become ready",
            )
            route_path.write_text(
                f"{self.route_token}: 127.0.0.1:{self.allocation.rfb_port}\n",
                encoding="ascii",
            )
            route_path.chmod(0o600)
            chrome = self.config.chrome_executable or _discover_chrome()
            entry_url = str(
                (self.contract.get("interactive") or {}).get("entry_url") or ""
            )
            if not entry_url.startswith(f"{self.challenge['origin']}/"):
                raise ValueError("interactive login entry URL is outside the bound origin")
            processes.append(
                _start_process(
                    [
                        str(chrome),
                        f"--user-data-dir={profile_dir}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-sync",
                        "--disable-extensions",
                        "--disable-dev-shm-usage",
                        "--password-store=basic",
                        "--use-mock-keychain",
                        "--no-sandbox",
                        "--window-position=0,0",
                        "--window-size=900,1100",
                        "--remote-debugging-address=127.0.0.1",
                        f"--remote-debugging-port={self.allocation.cdp_port}",
                        entry_url,
                    ],
                    environment,
                )
            )
            _wait_until(
                lambda: _port_ready("127.0.0.1", self.allocation.cdp_port),
                processes,
                "Chromium debugging endpoint did not become ready",
                timeout_seconds=20,
            )
            self.gateway_alive()
            self._set_verification_status("awaiting_login")
            self._ready.set()
            cdp_endpoint = f"http://127.0.0.1:{self.allocation.cdp_port}"
            with self.worker_factory(self.session, self.adapter, cdp_endpoint) as worker:
                while not self._stop.is_set() and time.monotonic() < deadline:
                    self.gateway_alive()
                    _require_processes_alive(processes)
                    try:
                        probe = self.adapter.probe_session(worker)
                    except AdapterLoginRequired:
                        self._set_verification_status("awaiting_login")
                    except AdapterSessionCheckUnavailable:
                        self._set_verification_status("verification_deferred")
                    else:
                        self._set_verification_status("verified")
                        active = self.session_registry.activate(
                            self.session["session_id"],
                            observed_principal_ref=probe.get(
                                "observed_principal_ref"
                            ),
                            source="remote_browser_login",
                        )
                        self.session_state_store.save(
                            self.session["session_id"],
                            worker.capture_session_state(),
                        )
                        self.challenge_store.complete(
                            self.challenge["challenge_id"],
                            result={
                                "session_id": active["session_id"],
                                "observed_principal_ref": active[
                                    "downstream_principal_ref"
                                ],
                                "template_count": None,
                            },
                        )
                        return
                    self._stop.wait(2)
            if self._stop.is_set():
                self._finish_failed(
                    "REMOTE_BROWSER_CANCELLED",
                    "The trusted remote browser login was cancelled.",
                )
            else:
                self._finish_failed(
                    "REMOTE_BROWSER_TIMEOUT",
                    "The trusted remote browser login timed out.",
                )
        except SessionPrincipalMismatch:
            self._finish_failed(
                "PRINCIPAL_MISMATCH",
                "The authenticated identity did not match the expected identity.",
                quarantine=True,
            )
        except Exception:
            logger.exception(
                "Remote browser failed for challenge %s",
                self.challenge["challenge_id"],
            )
            self._finish_failed(
                "REMOTE_BROWSER_FAILED",
                "The trusted remote browser login failed.",
            )
        finally:
            self._ready.set()
            for process in reversed(processes):
                _terminate_process(process)
            try:
                route_path.unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(runtime_dir, ignore_errors=True)
            self._done.set()
            self.on_finished()

    def _finish_failed(
        self,
        code: str,
        message: str,
        *,
        quarantine: bool = False,
    ) -> None:
        try:
            if quarantine:
                self.session_registry.quarantine(
                    self.session["session_id"],
                    message,
                    source="remote_browser_login",
                )
            else:
                self.session_registry.mark_expired(
                    self.session["session_id"],
                    message,
                    source="remote_browser_login",
                )
        except Exception:
            pass
        try:
            self.session_state_store.delete(self.session["session_id"])
        except SessionSecretError:
            pass
        try:
            self.challenge_store.fail(
                self.challenge["challenge_id"],
                code=code,
                message=message,
            )
        except Exception:
            pass


def _remote_url(base_url: str, *, route_token: str, password: str) -> str:
    path = "websockify?" + urlencode({"token": route_token})
    query = urlencode(
        {
            "autoconnect": "1",
            "resize": "scale",
            "scale": "true",
            "path": path,
        }
    )
    # Debian's noVNC vnc_lite parser only recognizes a fragment value after
    # "&". Keep an inert first value so the VNC password is supplied
    # automatically without putting it in the HTTP request or server logs.
    fragment = urlencode({"agentbridge": "1", "password": password})
    return (
        f"{base_url.rstrip('/')}/vnc_lite.html?{query}"
        f"#{fragment}"
    )


def _discover_chrome() -> Path:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        executable = shutil.which(name)
        if executable:
            return Path(executable)
    cache_root = Path.home() / ".cache" / "ms-playwright"
    matches = sorted(cache_root.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
    if matches:
        return matches[0]
    raise RuntimeError("a normal Chromium executable is required")


def _create_xauthority(path: Path, display: int) -> None:
    _require_command("xauth")
    cookie = secrets.token_hex(16)
    subprocess.run(
        ["xauth", "-f", str(path), "add", f":{display}", ".", cookie],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    path.chmod(0o600)


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required remote browser command is unavailable: {name}")


def _start_process(command: list[str], environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    )


def _wait_until(
    predicate: Callable[[], bool],
    processes: list[subprocess.Popen | None],
    message: str,
    *,
    timeout_seconds: float = 12,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _require_processes_alive(process for process in processes if process is not None)
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(message)


def _require_processes_alive(processes) -> None:
    for process in processes:
        if process.poll() is not None:
            raise RuntimeError("a remote browser subprocess exited unexpectedly")


def _port_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _remaining_challenge_seconds(challenge: dict) -> float:
    try:
        expires = datetime.fromisoformat(challenge["expires_at"])
    except (KeyError, TypeError, ValueError):
        return 0
    now = datetime.now(expires.tzinfo or timezone.utc)
    return max(0, (expires - now).total_seconds())
