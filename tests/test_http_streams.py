import socket
import ssl
from unittest.mock import Mock, patch

import pytest

from agentbridge.core.http_streams import peer_disconnected


def test_plain_peer_close_is_detected_without_changing_timeout():
    server, client = socket.socketpair()
    try:
        server.settimeout(7.0)
        assert peer_disconnected(server) is False
        client.close()
        assert peer_disconnected(server) is True
        assert server.gettimeout() == 7.0
    finally:
        server.close()
        client.close()


@pytest.mark.parametrize("error", [ssl.SSLWantReadError(), ssl.SSLWantWriteError(), BlockingIOError()])
def test_tls_control_record_is_not_a_disconnect_and_timeout_is_restored(error):
    connection = Mock()
    connection.pending.return_value = 0
    connection.gettimeout.return_value = 3.5
    connection.recv.side_effect = error
    with patch("agentbridge.core.http_streams.select.select", return_value=([connection], [], [])):
        assert peer_disconnected(connection) is False
    connection.recv.assert_called_once_with(1)  # No MSG_PEEK on SSL sockets.
    connection.settimeout.assert_called_once_with(3.5)


def test_tls_buffered_close_and_closed_descriptor_are_handled():
    connection = Mock()
    connection.pending.return_value = 1
    connection.recv.return_value = b""
    connection.gettimeout.return_value = None
    with patch("agentbridge.core.http_streams.select.select") as probe:
        assert peer_disconnected(connection) is True
    probe.assert_not_called()
    connection.settimeout.assert_called_once_with(None)
    with patch("agentbridge.core.http_streams.select.select", side_effect=ValueError("closed descriptor")):
        closed = Mock()
        closed.pending.return_value = 0
        assert peer_disconnected(closed) is True
