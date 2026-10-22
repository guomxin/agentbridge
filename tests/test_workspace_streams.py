from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import http.client
import json
import ssl
import threading
import time
from urllib.parse import urlencode
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config, SESSION_COOKIE
from tests.test_workspace import _create_account, _free_port, _request, _service, _wait_until
from tests.test_workspace_current import _message, _task


@contextmanager
def _server(tmp_path, *, tls=False):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    app = WorkspaceApplication(service=service)
    session = service.workspace.create_session(account["account_id"])
    cert_path = key_path = None
    if tls:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        key_path.chmod(0o600)
    port = _free_port()
    config = validate_workspace_server_config(host="127.0.0.1", port=port,
        public_base_url=f"{'https' if tls else 'http'}://127.0.0.1:{port}", tls_cert=cert_path, tls_key=key_path)
    server = create_workspace_http_server(config=config, application=app)
    server.handle_error = Mock()
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    worker.start()
    clients = []

    def connect(path="/api/timeline/stream?after=0"):
        conn = (http.client.HTTPSConnection("127.0.0.1", port, timeout=3,
                    context=ssl._create_unverified_context()) if tls
                else http.client.HTTPConnection("127.0.0.1", port, timeout=3))
        conn.request("GET", path, headers={"Cookie": f"{SESSION_COOKIE}={session['session_token']}"})
        response = conn.getresponse()
        clients.append((conn, response))
        return conn, response

    try:
        yield app, account, session, server, connect
    finally:
        for conn, response in clients:
            response.close()
            conn.close()
        _wait_until(lambda: not server._stream_counts, timeout=2)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _event(response):
    while True:
        lines = []
        while True:
            line = response.readline()
            if not line:
                return None
            if line == b"\n":
                break
            lines.append(line.decode().rstrip())
        event = dict(line.split(": ", 1) for line in lines if ": " in line and not line.startswith(":"))
        if "event" in event:
            if "data" in event:
                event["data"] = json.loads(event["data"])
            return event


@pytest.mark.parametrize("tls", [False, True])
def test_closed_six_streams_release_quota_for_new_seventh_without_server_errors(tmp_path, tls):
    with _server(tmp_path, tls=tls) as (app, account, session, server, connect):
        opened = [connect("/api/events/stream" if i % 2 else "/api/timeline/stream?after=0") for i in range(6)]
        assert all(response.status == 200 for _, response in opened)
        assert _wait_until(lambda: server._stream_counts.get(account["user_subject"]) == 6, timeout=1)
        _, rejected = connect()
        assert rejected.status == 429
        assert json.loads(rejected.read())["error"]["code"] == "STREAM_LIMIT_REACHED"
        started = time.monotonic()
        for connection, response in opened:
            response.close()
            connection.close()
        assert _wait_until(lambda: not server._stream_counts, timeout=1.5)
        assert time.monotonic() - started < 1.5
        _, replacement = connect()
        assert replacement.status == 200
        server.handle_error.assert_not_called()


@pytest.mark.parametrize("revoke", ["logout", "endpoint", "expiry", "binding_drift", "account_disabled"])
def test_live_session_revalidation_closes_idle_stream_and_releases_slot(tmp_path, revoke):
    # A heartbeat must revalidate even when the durable fallback interval is far away.
    with patch("agentbridge.workspace.server.WORKSPACE_STREAM_HEARTBEAT_SECONDS", .1), \
         patch("agentbridge.workspace.server.WORKSPACE_STREAM_RECHECK_SECONDS", 10), \
         _server(tmp_path) as (app, account, session, server, connect):
        from agentbridge.workspace.server import wait_for_change
        heartbeat = b": keepalive\n\n"
        heartbeats, writes_after_revocation = [], []
        paused, resume, revoked = threading.Event(), threading.Event(), threading.Event()
        durable_stream = server.RequestHandlerClass._durable_stream

        def observe_stream(handler, *args, **kwargs):
            write = handler.wfile.write

            def observed_write(data):
                result = write(data)
                if revoked.is_set():
                    writes_after_revocation.append(data)
                if data == heartbeat:
                    heartbeats.append(data)
                return result

            with patch.object(handler.wfile, "write", side_effect=observed_write):
                return durable_stream(handler, *args, **kwargs)

        def pause_after_buffered_heartbeats(*args, **kwargs):
            if len(heartbeats) == 2 and not paused.is_set():
                paused.set()
                assert resume.wait(3), "test did not release the stream after revocation"
                return
            return wait_for_change(*args, **kwargs)

        with patch.object(server.RequestHandlerClass, "_durable_stream", observe_stream), \
             patch("agentbridge.workspace.server.wait_for_change", side_effect=pause_after_buffered_heartbeats):
            try:
                _, response = connect("/api/timeline/stream")
                assert _event(response)["event"] == "cursor"
                # Reproduce the CI schedule exactly: legal pre-revocation heartbeats
                # remain unread while the stream is held at its next wait boundary.
                assert paused.wait(3), "stream did not emit the two legal heartbeats"
                if revoke == "logout":
                    app.logout(session["session_token"])
                elif revoke == "endpoint":
                    with app.service.tasks._connect() as connection:
                        connection.execute("UPDATE client_endpoints SET state = 'disabled' WHERE endpoint_id = ?", (account["endpoint_id"],))
                elif revoke == "binding_drift":
                    with app.service.tasks._connect() as connection:
                        connection.execute("UPDATE client_endpoints SET conversation_ref = 'changed' WHERE endpoint_id = ?", (account["endpoint_id"],))
                elif revoke == "account_disabled":
                    with app.store._connect() as connection:
                        connection.execute("UPDATE workspace_accounts SET state = 'disabled' WHERE account_id = ?", (account["account_id"],))
                else:
                    with app.store._connect() as connection:
                        connection.execute("UPDATE workspace_sessions SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
                revoked.set()
                assert response.read(len(heartbeat) * 2) == heartbeat * 2
                started = time.monotonic()
                resume.set()
                # Read one byte, not an unbounded body: a broken stream that keeps
                # emitting heartbeats must fail immediately instead of hanging.
                assert response.read(1) == b""
                assert time.monotonic() - started < 1
                assert _wait_until(lambda: not server._stream_counts, timeout=1)
                assert writes_after_revocation == []
                server.handle_error.assert_not_called()
            finally:
                resume.set()


def test_stream_durable_commit_wakes_immediately_and_rollback_never_appears(tmp_path):
    with _server(tmp_path) as (app, account, session, server, connect):
        _, response = connect("/api/timeline/stream")
        assert _event(response)["event"] == "cursor"
        with app.service.tasks._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            app.service.tasks._append_timeline_message_in_connection(connection, user_subject=account["user_subject"],
                source_endpoint_id=account["endpoint_id"], message_key="rolled-back", role="assistant", text="Rollback")
            connection.rollback()
        started = time.monotonic()
        appended = _message(app.service, account, "committed")
        event = _event(response)
        assert event["event"] == "timeline" and event["data"]["sequence"] == appended["sequence"]
        assert event["data"]["message_key"] == "committed"
        assert time.monotonic() - started < 1
        assert len(app.list_timeline(account, entry_type="chat_message")) == 1
        # Waiting readers do not retain a transaction or block a writer.
        with app.service.tasks._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()


def test_stream_recovers_without_process_local_hint_and_plans_cursor_safe_reconnect(tmp_path):
    with patch("agentbridge.workspace.server.WORKSPACE_STREAM_LIFETIME_SECONDS", 2.4), \
         patch.dict("os.environ", {"AGENTBRIDGE_NOTIFICATION_WAKEUPS": "0"}), \
         _server(tmp_path) as (app, account, session, server, connect):
        _, response = connect("/api/timeline/stream")
        assert _event(response)["id"] == "0"
        appended = _message(app.service, account, "cross-process")
        event = _event(response)
        assert event["event"] == "timeline" and event["id"] == str(appended["sequence"])
        reconnect = _event(response)
        assert reconnect == {"id": str(appended["sequence"]), "event": "reconnect", "data": {"planned": True}}
        assert response.read() == b""
        assert _wait_until(lambda: not server._stream_counts, timeout=1)
        _, resumed = connect(f"/api/timeline/stream?after={appended['sequence']}")
        again = _event(resumed)
        assert again["event"] == "reconnect"  # No duplicate replay of the acknowledged row.


def test_new_current_projection_and_history_http_routes_validate_and_scope(tmp_path):
    with _server(tmp_path) as (app, account, session, server, connect):
        task = _task(app.service, account)
        cookies = {SESSION_COOKIE: session["session_token"]}
        status, _, snapshot = _request(server.server_port, "GET", "/api/current", cookies=cookies)
        assert status == 200 and snapshot["cards"][0]["task"]["task_id"] == task["task_id"]
        query = urlencode({"ids": task["task_id"], "versions": task["version"]})
        status, _, result = _request(server.server_port, "GET", "/api/tasks/projections?" + query, cookies=cookies)
        assert status == 200 and result["items"][0]["unchanged"] is False
        status, _, result = _request(server.server_port, "GET", "/api/tasks/history?limit=32", cookies=cookies)
        assert status == 200 and result["items"][0]["task_id"] == task["task_id"]
        for path in ["/api/tasks/projections", "/api/tasks/projections?ids=bad", "/api/tasks/projections?ids=" + task["task_id"] + "&versions=bad",
                     "/api/tasks/history?before_task_id=" + task["task_id"]]:
            assert _request(server.server_port, "GET", path, cookies=cookies)[0] == 400
        assert _request(server.server_port, "GET", "/api/current")[0] == 401
