"""Process-owned POSIX report leases, released even after forced termination."""
from contextlib import contextmanager
import fcntl


@contextmanager
def report_lease(path, *, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as stream:
        acquired = False
        try:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(stream, fcntl.LOCK_UN)
