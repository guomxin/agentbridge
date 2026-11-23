"""Read a Bot API secret from one explicitly configured private source."""

import ctypes
import os
import platform
import pwd
import re
from pathlib import Path

from .private_files import private_text


def _login_keychain_item(service, account):
    """Read one generic-password item using the ACL of this signed Python executable."""
    library = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    pointer = ctypes.c_void_p
    number = ctypes.c_uint32
    library.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(pointer)]
    library.SecKeychainFindGenericPassword.argtypes = [
        pointer,
        number,
        ctypes.c_char_p,
        number,
        ctypes.c_char_p,
        ctypes.POINTER(number),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    library.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    library.SecKeychainGetUserInteractionAllowed.argtypes = [
        ctypes.POINTER(ctypes.c_ubyte)
    ]
    library.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
    cf.CFRelease.argtypes = [pointer]
    cf.CFRelease.restype = None
    location = (
        Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Keychains/login.keychain-db"
    )
    keychain = pointer()
    interaction = ctypes.c_ubyte()
    content = pointer()
    size = number()
    changed = False
    try:
        if library.SecKeychainGetUserInteractionAllowed(ctypes.byref(interaction)):
            raise ValueError("Keychain access unavailable")
        if library.SecKeychainSetUserInteractionAllowed(0):
            raise ValueError("Cannot query unattended Keychain access")
        changed = True
        if library.SecKeychainOpen(os.fsencode(location), ctypes.byref(keychain)):
            raise ValueError("Login Keychain unavailable")
        service, account = service.encode(), account.encode()
        status = library.SecKeychainFindGenericPassword(
            keychain,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(size),
            ctypes.byref(content),
            None,
        )
        if status or not content or not 0 < size.value <= 4096:
            raise ValueError("Could not read the configured Telegram Keychain item")
        return ctypes.string_at(content, size.value).decode("ascii").strip()
    finally:
        if content:
            library.SecKeychainItemFreeContent(None, content)
        if changed:
            library.SecKeychainSetUserInteractionAllowed(interaction.value)
        if keychain:
            cf.CFRelease(keychain)


def bot_token(config):
    if ("botTokenFile" in config) == ("botCredential" in config):
        raise ValueError("Choose exactly one Telegram credential source")
    if "botTokenFile" in config:
        return private_text(Path(config["botTokenFile"])).strip()
    reference = config["botCredential"]
    if not isinstance(reference, dict):
        raise TypeError("Invalid Telegram credential reference")
    if reference.get("kind") == "macos-keychain" and set(reference) == {
        "kind",
        "service",
        "account",
    }:
        if platform.system() != "Darwin":
            raise ValueError("This Telegram credential requires a macOS login keychain")
        for key in ("service", "account"):
            value = reference[key]
            if (
                not isinstance(value, str)
                or not value
                or not value.isprintable()
                or len(value) > 255
            ):
                raise ValueError("Invalid Keychain reference")
        try:
            return _login_keychain_item(reference["service"], reference["account"])
        except (OSError, UnicodeError, ValueError):
            raise ValueError(
                "Could not read the configured Telegram Keychain item"
            ) from None
    if reference.get("kind") == "systemd" and set(reference) == {"kind", "name"}:
        name = reference["name"]
        path = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
        if (
            platform.system() != "Linux"
            or not path.is_absolute()
            or not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,127}", name)
        ):
            raise ValueError("Invalid systemd credential reference")
        return private_text(path / name).strip()
    raise ValueError("Invalid Telegram credential reference")
