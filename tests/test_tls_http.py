from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import http.client
import socket
import ssl
import threading
import time
import unittest

from agentbridge.core.tls_http import ThreadedTLSHTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format: str, *_args) -> None:
        return None


class _BlockingFirstContext:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._calls = 0
        self.observed_timeouts: list[float | None] = []

    def wrap_socket(self, request: socket.socket, *, server_side: bool):
        self.observed_timeouts.append(request.gettimeout())
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.entered.set()
            self.release.wait(timeout=3)
            raise ssl.SSLError("stalled handshake")
        return request


class ThreadedTLSHTTPServerTests(unittest.TestCase):
    def test_idle_and_slow_drip_connections_release_bounded_capacity(self):
        server = ThreadedTLSHTTPServer(('127.0.0.1', 0), _HealthHandler,
            max_connections=2, max_connections_per_peer=1,
            socket_timeout_seconds=.3, connection_lifetime_seconds=.7)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        slow = socket.create_connection(server.server_address, timeout=2)
        try:
            slow.sendall(b'GET / HTTP/1.1\r\nX-Slow: ')
            deadline = time.monotonic() + 1.5
            while not server._peer_counts and time.monotonic() < deadline:
                time.sleep(.01)
            rejected = socket.create_connection(server.server_address, timeout=2)
            try:
                self.assertEqual(rejected.recv(1), b'')
            finally:
                rejected.close()
            # Activity faster than the idle timeout cannot defeat the absolute budget.
            for _ in range(10):
                try:
                    slow.sendall(b'a')
                except OSError:
                    break
                time.sleep(.1)
            while server._peer_counts and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(server._peer_counts, {})
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request('GET', '/healthz')
            response = connection.getresponse()
            self.assertEqual(response.read(), b'ok')
            connection.close()
        finally:
            slow.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_stream_budget_is_per_authenticated_subject_and_released_on_failure(self):
        from agentbridge.workspace.server import WorkspaceHTTPServer, _bounded_user_stream
        server = WorkspaceHTTPServer(('127.0.0.1', 0), _HealthHandler)
        try:
            for _ in range(6):
                self.assertTrue(server.acquire_stream('alice'))
            self.assertFalse(server.acquire_stream('alice'))
            self.assertTrue(server.acquire_stream('bob'))
            class Handler:
                @_bounded_user_stream
                def stream(self, account):
                    raise BrokenPipeError()
            handler = Handler()
            handler.server = server
            with self.assertRaises(BrokenPipeError):
                handler.stream({'user_subject': 'charlie'})
            self.assertNotIn('charlie', server._stream_counts)
            for _ in range(6):
                server.release_stream('alice')
            server.release_stream('bob')
            self.assertEqual(server._stream_counts, {})
        finally:
            server.server_close()

    def test_stalled_tls_handshake_does_not_block_accept_loop(self) -> None:
        context = _BlockingFirstContext()
        server = ThreadedTLSHTTPServer(
            ("127.0.0.1", 0),
            _HealthHandler,
            tls_handshake_timeout_seconds=0.5,
        )
        server.enable_tls(context)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stalled = socket.create_connection(server.server_address, timeout=2)
        try:
            self.assertTrue(context.entered.wait(timeout=2))
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
                timeout=2,
            )
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
            connection.close()
            self.assertEqual(context.observed_timeouts, [0.5, 0.5])
        finally:
            context.release.set()
            stalled.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
