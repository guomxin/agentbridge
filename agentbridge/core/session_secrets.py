from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SESSION_KEY_FILE_ENV = "AGENTBRIDGE_SESSION_KEY_FILE"
_AES_GCM_KEY_BYTES = 32
_AES_GCM_NONCE_BYTES = 12
_AES_GCM_TAG_BYTES = 16
_AES_GCM_ENVELOPE = b"ABSS\x01"
_AES_GCM_AAD_PREFIX = b"agentbridge.session-state.v1\x00"


class SessionSecretError(RuntimeError):
    pass


class SessionStateAccessDenied(SessionSecretError):
    """The encrypted state is not usable by the current security context."""


class SessionStateProtector(Protocol):
    def protect(self, plaintext: bytes, *, context: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes: ...


class SessionStateStore:
    def __init__(
        self,
        root: Path | str,
        *,
        protector: SessionStateProtector | None = None,
    ) -> None:
        self.root = Path(root)
        self.protector = protector or _default_protector()

    def save(self, session_id: str, state: dict) -> None:
        _validate_state(state)
        plaintext = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = self.protector.protect(plaintext, context=session_id.encode("utf-8"))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(session_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise SessionSecretError("could not persist encrypted session state") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self, session_id: str) -> dict | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            ciphertext = path.read_bytes()
            plaintext = self.protector.unprotect(
                ciphertext,
                context=session_id.encode("utf-8"),
            )
            state = json.loads(plaintext.decode("utf-8"))
        except SessionSecretError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionSecretError("encrypted session state could not be loaded") from exc
        _validate_state(state)
        return state

    def delete(self, session_id: str) -> None:
        try:
            self.path_for(session_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SessionSecretError("encrypted session state could not be deleted") from exc

    def path_for(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.bin"


class AesGcmSessionStateProtector:
    def __init__(self, key: bytes) -> None:
        if len(key) != _AES_GCM_KEY_BYTES:
            raise SessionSecretError("session-state key must contain exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_key_file(cls, path: Path | str) -> "AesGcmSessionStateProtector":
        key = _read_session_key_file(path)
        try:
            return cls(bytes(key))
        finally:
            for index in range(len(key)):
                key[index] = 0

    def protect(self, plaintext: bytes, *, context: bytes) -> bytes:
        nonce = os.urandom(_AES_GCM_NONCE_BYTES)
        encrypted = self._cipher.encrypt(
            nonce,
            plaintext,
            _AES_GCM_AAD_PREFIX + context,
        )
        return _AES_GCM_ENVELOPE + nonce + encrypted

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes:
        minimum_size = (
            len(_AES_GCM_ENVELOPE) + _AES_GCM_NONCE_BYTES + _AES_GCM_TAG_BYTES
        )
        if len(ciphertext) < minimum_size or not ciphertext.startswith(
            _AES_GCM_ENVELOPE
        ):
            raise SessionSecretError("encrypted session state envelope is invalid")
        nonce_start = len(_AES_GCM_ENVELOPE)
        nonce_end = nonce_start + _AES_GCM_NONCE_BYTES
        nonce = ciphertext[nonce_start:nonce_end]
        encrypted = ciphertext[nonce_end:]
        try:
            return self._cipher.decrypt(
                nonce,
                encrypted,
                _AES_GCM_AAD_PREFIX + context,
            )
        except InvalidTag as exc:
            raise SessionStateAccessDenied(
                "encrypted session state could not be authenticated"
            ) from exc


def _default_protector() -> SessionStateProtector:
    if os.name == "posix":
        key_file = os.environ.get(SESSION_KEY_FILE_ENV)
        if not key_file:
            raise SessionSecretError(
                f"{SESSION_KEY_FILE_ENV} is required on this operating system"
            )
        return AesGcmSessionStateProtector.from_key_file(key_file)
    raise SessionSecretError(
        "no session-state protector is configured for this operating system"
    )


def _read_session_key_file(path: Path | str) -> bytearray:
    key_path = Path(path)
    if not key_path.is_absolute():
        raise SessionSecretError("session-state key file path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        raise SessionSecretError("session-state key file could not be opened") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SessionSecretError("session-state key path must be a regular file")
        if os.name == "posix":
            permissions = stat.S_IMODE(metadata.st_mode)
            allowed = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
            if permissions & ~allowed:
                raise SessionSecretError(
                    "session-state key file permissions are too broad"
                )
            if metadata.st_uid not in {0, os.geteuid()}:
                raise SessionSecretError(
                    "session-state key file owner is not trusted"
                )
        key = bytearray()
        while len(key) <= _AES_GCM_KEY_BYTES:
            chunk = os.read(descriptor, _AES_GCM_KEY_BYTES + 1 - len(key))
            if not chunk:
                break
            key.extend(chunk)
    except OSError as exc:
        raise SessionSecretError("session-state key file could not be read") from exc
    finally:
        os.close(descriptor)
    if len(key) != _AES_GCM_KEY_BYTES:
        for index in range(len(key)):
            key[index] = 0
        raise SessionSecretError("session-state key must contain exactly 32 bytes")
    return key


def _validate_state(state: object) -> None:
    if not isinstance(state, dict):
        raise SessionSecretError("session state must be an object")
    cookies = state.get("cookies")
    if not isinstance(cookies, list) or not all(isinstance(cookie, dict) for cookie in cookies):
        raise SessionSecretError("session state must contain a cookie list")
