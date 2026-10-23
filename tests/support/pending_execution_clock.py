"""Bound a negative readback test by an event, not a count of clock reads."""
from contextlib import contextmanager
from unittest.mock import patch

from agentbridge.adapters import seeyon_pending_execution as execution


@contextmanager
def single_readback_attempt():
    clock = [0.0]
    verify = execution.verify_pending_action

    def once(*args, **kwargs):
        try:
            return verify(*args, **kwargs)
        finally:
            clock[0] = 61.0

    with patch.object(execution.time, "monotonic", lambda: clock[0]), patch.object(
        execution, "verify_pending_action", once
    ):
        yield
