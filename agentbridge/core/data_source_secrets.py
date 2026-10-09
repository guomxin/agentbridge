"""Purpose-separated encrypted JSON; never serialized as browser session state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from agentbridge.core.session_secrets import SessionStateProtector, _default_protector


class ProtectedJsonStore:
    def __init__(self, root: Path | str, *, purpose: str,
                 protector: SessionStateProtector | None = None) -> None:
        self.root = Path(root)
        self.purpose = purpose.encode("utf-8") + b"\x00"
        self.protector = protector or _default_protector()

    def path(self, key: str) -> Path:
        return self.root / (hashlib.sha256(self.purpose + key.encode()).hexdigest() + ".bin")

    def save(self, key: str, payload: dict) -> None:
        ciphertext = self.protector.protect(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(),
            context=self.purpose + key.encode(),
        )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(key)
        temporary = path.with_name(uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, key: str) -> dict:
        data = self.protector.unprotect(self.path(key).read_bytes(), context=self.purpose + key.encode())
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("invalid protected record")
        return value

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


class DataSourceSecretStore(ProtectedJsonStore):
    def __init__(self, root, *, protector=None):
        super().__init__(root, purpose="agentbridge.datasource.v1", protector=protector)
