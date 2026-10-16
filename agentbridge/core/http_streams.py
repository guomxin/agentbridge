"""Connection liveness for completed, non-reusable HTTP stream requests."""
from __future__ import annotations

import select
import ssl


CLIENT_DISCONNECT_ERRORS = (
    TimeoutError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
    ssl.SSLEOFError, ssl.SSLZeroReturnError,
)


def peer_disconnected(connection) -> bool:
    """Probe a fully-read request without blocking or using TLS MSG_PEEK.

    Call only after consuming the complete request, with Connection: close.
    TLS control records may make a socket readable without application data;
    SSLWantRead/SSLWantWrite therefore mean no confirmed disconnect. The
    original timeout is restored before the caller writes another response.
    """
    try:
        pending = getattr(connection, "pending", None)
        if not (callable(pending) and pending() > 0) and not select.select([connection], [], [], 0)[0]:
            return False
        timeout = connection.gettimeout()
        disconnected = False
        try:
            connection.setblocking(False)
            disconnected = connection.recv(1) == b""
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError, InterruptedError):
            pass
        except (OSError, ValueError):
            disconnected = True
        finally:
            try:
                connection.settimeout(timeout)
            except (OSError, ValueError):
                disconnected = True
        return disconnected
    except (OSError, ValueError):
        return True
