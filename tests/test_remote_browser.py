from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.base import AdapterLoginRequired
from agentbridge.broker.remote_browser import (
    RemoteBrowserConfig,
    RemoteInteractiveBrowserBroker,
    _NoVncGateway,
    _RemoteBrowserAllocation,
    _RemoteInteractiveRun,
    _remote_url,
)
from agentbridge.core.auth_challenges import AuthChallengeStore
from agentbridge.core.sessions import SessionRegistry


class RemoteBrowserConfigTests(unittest.TestCase):
    def test_requires_https_for_non_loopback_unless_explicitly_allowed(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "must use HTTPS"):
                RemoteBrowserConfig(
                    runtime_root=Path(tmp) / "runtime",
                    public_base_url="http://10.10.50.213:8781",
                    listen_host="10.10.50.213",
                    listen_port=8781,
                    tls_cert=None,
                    tls_key=None,
                )

            config = RemoteBrowserConfig(
                runtime_root=Path(tmp) / "runtime",
                public_base_url="http://10.10.50.213:8781",
                listen_host="10.10.50.213",
                listen_port=8781,
                tls_cert=None,
                tls_key=None,
                allow_insecure_private_http=True,
            )

            self.assertTrue(config.allow_insecure_private_http)

    def test_rejects_wildcard_listener_and_mismatched_public_url(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "explicit"):
                RemoteBrowserConfig(
                    runtime_root=Path(tmp) / "runtime",
                    public_base_url="http://127.0.0.1:8781",
                    listen_host="0.0.0.0",
                    listen_port=8781,
                    tls_cert=None,
                    tls_key=None,
                )
            with self.assertRaisesRegex(ValueError, "match the listen host"):
                RemoteBrowserConfig(
                    runtime_root=Path(tmp) / "runtime",
                    public_base_url="http://localhost:8781",
                    listen_host="127.0.0.1",
                    listen_port=8781,
                    tls_cert=None,
                    tls_key=None,
                )

    def test_remote_url_keeps_password_in_fragment_and_routes_by_opaque_token(self):
        value = _remote_url(
            "https://10.10.50.213:8781",
            route_token="opaque-route",
            password="secret42",
        )
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)

        self.assertEqual(fragment["agentbridge"], ["1"])
        self.assertEqual(fragment["password"], ["secret42"])
        self.assertIn("&password=", parsed.fragment)
        self.assertNotIn("secret42", parsed.query)
        self.assertEqual(
            query["path"],
            ["websockify?token=opaque-route"],
        )
        self.assertEqual(query["autoconnect"], ["1"])


class RemoteBrowserBrokerTests(unittest.TestCase):
    def test_allocations_are_isolated_and_runtime_root_is_marked(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker = _broker(root)

            first = broker._allocate()
            second = broker._allocate()

            self.assertNotEqual(first.display, second.display)
            self.assertNotEqual(first.rfb_port, second.rfb_port)
            self.assertNotEqual(first.cdp_port, second.cdp_port)
            self.assertTrue(
                (root / "remote" / ".agentbridge-remote-browser-root").is_file()
            )
            broker._release("challenge-a", first.slot)
            broker._release("challenge-b", second.slot)
            broker.shutdown()

    def test_refuses_to_clean_an_unmarked_nonempty_runtime_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "remote"
            runtime.mkdir()
            (runtime / "foreign.txt").write_text("keep", encoding="ascii")

            with self.assertRaisesRegex(ValueError, "not AgentBridge-managed"):
                _broker(root)

            self.assertTrue((runtime / "foreign.txt").is_file())


class RemoteBrowserRunTests(unittest.TestCase):
    def test_direct_chromium_login_saves_verified_session_and_cleans_runtime(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthChallengeStore(root / "agentbridge.db")
            sessions = SessionRegistry(root / "agentbridge.db", root / "profiles")
            session = sessions.get_or_create(
                user_subject="user-a",
                system_id="yuque",
                expected_principal_ref="辛国茂",
            )
            challenge = _challenge(store, session["session_id"])
            csrf = store.issue_csrf(challenge["challenge_id"])
            challenge = store.claim(
                challenge["challenge_id"],
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            config = _config(root)
            config.runtime_root.mkdir(mode=0o700)
            (config.runtime_root / "sessions").mkdir(mode=0o700)
            token_directory = config.runtime_root / "tokens"
            token_directory.mkdir(mode=0o700)
            state_store = FakeStateStore()
            adapter = FakeAdapter()
            commands: list[list[str]] = []
            finished: list[bool] = []

            def fake_start(command, _environment):
                commands.append(command)
                return FakeProcess()

            run = _RemoteInteractiveRun(
                challenge=challenge,
                session=session,
                contract=adapter.authentication_contract(),
                adapter=adapter,
                worker_factory=lambda *_args: fake_worker(),
                challenge_store=store,
                session_registry=sessions,
                session_state_store=state_store,
                config=config,
                allocation=_RemoteBrowserAllocation(
                    slot=0,
                    display=100,
                    rfb_port=5901,
                    cdp_port=9222,
                ),
                token_directory=token_directory,
                gateway_alive=lambda: None,
                timeout_seconds=30,
                on_finished=lambda: finished.append(True),
            )

            with (
                patch(
                    "agentbridge.broker.remote_browser._create_xauthority",
                    lambda path, _display: path.write_text("xauth", encoding="ascii"),
                ),
                patch(
                    "agentbridge.broker.remote_browser._start_process",
                    side_effect=fake_start,
                ),
                patch(
                    "agentbridge.broker.remote_browser._wait_until",
                    lambda *_args, **_kwargs: None,
                ),
                patch(
                    "agentbridge.broker.remote_browser._discover_chrome",
                    return_value=Path("/opt/chromium/chrome"),
                ),
            ):
                run.start()
                run.join(timeout_seconds=5)

            self.assertTrue(run.done)
            self.assertEqual(store.get(challenge["challenge_id"])["state"], "succeeded")
            self.assertEqual(
                sessions.get(session["session_id"])["downstream_principal_ref"],
                "辛国茂",
            )
            self.assertEqual(state_store.saved, {"cookies": [{"name": "session"}]})
            chrome_command = next(
                command
                for command in commands
                if "--remote-debugging-address=127.0.0.1" in command
            )
            self.assertIn("--remote-debugging-address=127.0.0.1", chrome_command)
            self.assertIn("--remote-debugging-port=9222", chrome_command)
            self.assertNotIn("--enable-automation", chrome_command)
            self.assertNotIn("--remote-debugging-pipe", chrome_command)
            x11vnc_command = next(
                command for command in commands if command[0] == "x11vnc"
            )
            self.assertIn("-localhost", x11vnc_command)
            self.assertFalse(
                (config.runtime_root / "sessions" / challenge["challenge_id"]).exists()
            )
            self.assertEqual(list(token_directory.iterdir()), [])
            self.assertEqual(finished, [True])

    def test_gateway_uses_one_tls_listener_and_reloadable_token_directory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            config.runtime_root.mkdir(mode=0o700)
            token_directory = config.runtime_root / "tokens"
            token_directory.mkdir(mode=0o700)
            config.novnc_web_root.mkdir()
            commands = []

            with (
                patch("agentbridge.broker.remote_browser._require_command"),
                patch(
                    "agentbridge.broker.remote_browser._start_process",
                    side_effect=lambda command, _environment: (
                        commands.append(command) or FakeProcess()
                    ),
                ),
                patch(
                    "agentbridge.broker.remote_browser._wait_until",
                    lambda *_args, **_kwargs: None,
                ),
            ):
                gateway = _NoVncGateway(config)
                gateway.ensure_started()
                gateway.stop()

            command = commands[0]
            self.assertIn("--token-plugin=TokenFile", command)
            self.assertIn(f"--token-source={token_directory}", command)
            self.assertIn("--ssl-only", command)
            self.assertEqual(command[-1], "10.10.50.213:8781")


    def test_gateway_terminates_half_started_process_when_listener_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config(root)
            config.runtime_root.mkdir(mode=0o700)
            (config.runtime_root / "tokens").mkdir(mode=0o700)
            config.novnc_web_root.mkdir()
            process = FakeProcess()

            with (
                patch("agentbridge.broker.remote_browser._require_command"),
                patch(
                    "agentbridge.broker.remote_browser._start_process",
                    return_value=process,
                ),
                patch(
                    "agentbridge.broker.remote_browser._wait_until",
                    side_effect=RuntimeError("listener failed"),
                ),
            ):
                gateway = _NoVncGateway(config)
                with self.assertRaisesRegex(RuntimeError, "listener failed"):
                    gateway.ensure_started()

            self.assertTrue(process.terminated)
            self.assertIsNone(gateway._process)

class FakeStateStore:
    def __init__(self) -> None:
        self.saved = None

    def delete(self, _session_id: str) -> None:
        self.saved = None

    def save(self, _session_id: str, state: dict) -> None:
        self.saved = state


class FakeAdapter:
    def authentication_contract(self) -> dict:
        return {
            "system_id": "yuque",
            "system_name": "部门信息库",
            "origin": "https://tc-aiot.yuque.com",
            "page_fingerprint": "yuque-interactive-login-v1",
            "authentication_mode": "interactive_browser",
            "fields": [],
            "interactive": {
                "entry_url": "https://tc-aiot.yuque.com/login",
            },
        }

    def probe_session(self, _worker) -> dict:
        return {
            "authenticated": True,
            "observed_principal_ref": "辛国茂",
        }


class FakeWorker:
    def capture_session_state(self) -> dict:
        return {"cookies": [{"name": "session"}]}


@contextmanager
def fake_worker():
    yield FakeWorker()


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        del timeout
        self.terminated = True
        return 0

    def kill(self):
        self.terminated = True


def _broker(root: Path) -> RemoteInteractiveBrowserBroker:
    store = AuthChallengeStore(root / "agentbridge.db")
    sessions = SessionRegistry(root / "agentbridge.db", root / "profiles")
    return RemoteInteractiveBrowserBroker(
        challenge_store=store,
        session_registry=sessions,
        session_state_store=FakeStateStore(),
        adapter_factory=lambda _challenge: FakeAdapter(),
        worker_factory=lambda *_args: fake_worker(),
        config=RemoteBrowserConfig(
            runtime_root=root / "remote",
            public_base_url="http://127.0.0.1:8781",
            listen_host="127.0.0.1",
            listen_port=8781,
            tls_cert=None,
            tls_key=None,
            display_start=800,
            rfb_port_start=45000,
            cdp_port_start=45100,
        ),
    )


def _config(root: Path) -> RemoteBrowserConfig:
    cert = root / "server.crt"
    key = root / "server.key"
    cert.write_text("cert", encoding="ascii")
    key.write_text("key", encoding="ascii")
    return RemoteBrowserConfig(
        runtime_root=root / "remote",
        public_base_url="https://10.10.50.213:8781",
        listen_host="10.10.50.213",
        listen_port=8781,
        tls_cert=cert,
        tls_key=key,
        chrome_executable=None,
        novnc_web_root=root / "novnc",
    )


def _challenge(store: AuthChallengeStore, session_id: str) -> dict:
    return store.create(
        user_subject="user-a",
        system_id="yuque",
        session_id=session_id,
        origin="https://tc-aiot.yuque.com",
        page_fingerprint="yuque-interactive-login-v1",
        nonce="nonce",
        fields=[],
        card_base_url="https://10.10.50.213:8780",
        system_name="部门信息库",
        expected_principal_ref="辛国茂",
        ttl_seconds=600,
        challenge_type="interactive_browser_login",
    )


if __name__ == "__main__":
    unittest.main()
