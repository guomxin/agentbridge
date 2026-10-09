from __future__ import annotations

from http.server import ThreadingHTTPServer
import socket
import ssl
import threading


class ThreadedTLSHTTPServer(ThreadingHTTPServer):
    """Keep slow TLS handshakes away from the accept loop."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        *args,
        tls_handshake_timeout_seconds: float = 10.0,
        socket_timeout_seconds: float = 30.0,
        connection_lifetime_seconds: float = 360.0,
        max_connections: int = 64,
        max_connections_per_peer: int = 16,
        **kwargs,
    ) -> None:
        self._tls_context: ssl.SSLContext | None = None
        self._tls_handshake_timeout_seconds = tls_handshake_timeout_seconds
        if min(tls_handshake_timeout_seconds, socket_timeout_seconds, connection_lifetime_seconds,
               max_connections, max_connections_per_peer) <= 0:
            raise ValueError("HTTP resource budgets must be positive")
        self._socket_timeout_seconds = socket_timeout_seconds
        self._connection_lifetime_seconds = connection_lifetime_seconds
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        self._max_connections_per_peer = max_connections_per_peer
        self._peer_counts: dict[str, int] = {}
        self._budget_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def enable_tls(self, context: ssl.SSLContext) -> None:
        self._tls_context = context

    def process_request(self, request, client_address) -> None:
        peer = client_address[0]  # Never trust forwarded headers for admission.
        with self._budget_lock:
            admitted = (self._peer_counts.get(peer, 0) < self._max_connections_per_peer
                        and self._connection_slots.acquire(blocking=False))
            if admitted:
                self._peer_counts[peer] = self._peer_counts.get(peer, 0) + 1
        if not admitted:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_budget(peer)
            raise

    def _release_budget(self, peer: str) -> None:
        with self._budget_lock:
            count = self._peer_counts[peer] - 1
            if count:
                self._peer_counts[peer] = count
            else:
                del self._peer_counts[peer]
            self._connection_slots.release()

    def process_request_thread(
        self,
        request: socket.socket,
        client_address,
    ) -> None:
        wrapped_request = request
        # Absolute lifetime prevents slow byte trickles from renewing idle timeouts forever.
        def expire():
            try:
                wrapped_request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(self._connection_lifetime_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            if self._tls_context is not None:
                request.settimeout(self._tls_handshake_timeout_seconds)
                wrapped_request = self._tls_context.wrap_socket(
                    request,
                    server_side=True,
                )
            wrapped_request.settimeout(self._socket_timeout_seconds)
            super().process_request_thread(wrapped_request, client_address)
        except (OSError, ssl.SSLError):
            wrapped_request.close()
            request.close()
        finally:
            timer.cancel()
            self._release_budget(client_address[0])
