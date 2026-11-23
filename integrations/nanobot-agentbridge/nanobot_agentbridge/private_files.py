"""Private files consumed by the separately installed NanoBot host."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def private_file(path: Path):
    if not path.is_absolute():
        raise ValueError("Expected an absolute private file path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("Private file must be owner-only")
        yield handle


def private_text(path: Path) -> str:
    with private_file(path) as handle:
        result = handle.read(1_048_577)
        if len(result) > 1_048_576:
            raise ValueError("Private file is too large")
        return result


def private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Expected an absolute private directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError("Expected an owner-only directory")
    return path
