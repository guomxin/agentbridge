"""Native maintenance primitives. No shell evaluation of local configuration."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]


def run(argv, *, cwd=ROOT, env=None, input=None, timeout=900, diagnostics=None):
    try:
        result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, input=input,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(str(argv[0])).name} timed out") from None
    if diagnostics:
        save(diagnostics, {'command': [str(a) for a in argv], 'exitCode': result.returncode,
                           'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode:
        # Child output can contain credentials. Keep it out of exception/chat logs.
        raise RuntimeError(f"{Path(str(argv[0])).name} failed (exit {result.returncode})")
    return result.stdout.strip()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def external(path, root=ROOT):
    value = Path(path).expanduser().resolve()
    if value.is_relative_to(root.resolve()):
        raise ValueError('Private material must be outside the repository')
    return value


def private_file(path):
    path = external(path)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Private file missing or accessible by group/others')
    return path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def environment_file(path):
    """Parse literal dotenv values, never source a shell file or expand commands."""
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        name, sep, value = line.partition('=')
        import re
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name.strip()):
            raise ValueError('Unsupported dotenv line')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
            value = value[1:-1]
        values[name.strip()] = value
    if values.get('NODE_TLS_REJECT_UNAUTHORIZED') == '0':
        raise ValueError('TLS verification must stay enabled')
    return values
