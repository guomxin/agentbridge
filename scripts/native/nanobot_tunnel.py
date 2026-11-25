"""Install one unprivileged reverse-only sshd identity after release approval."""

import base64
import hashlib
import os
import pwd
import re
import subprocess
import tempfile
from pathlib import Path


def tunnel_user(instance):
    return "abnb-" + hashlib.sha256(instance.encode()).hexdigest()[:12]


def tunnel_settings(spec):
    user = tunnel_user(spec["instanceId"])
    home = Path("/var/lib") / user
    port = spec["remotePort"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid SSH pilot port")
    key = spec["tunnelPublicKey"]
    parts = key.split()
    if (
        len(parts) != 2
        or parts[0] != "ssh-ed25519"
        or not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1])
    ):
        raise ValueError("Invalid dedicated SSH public identity")
    blob = base64.b64decode(parts[1], validate=True)
    if len(blob) != 51 or not blob.startswith(
        b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    ):
        raise ValueError("Expected an Ed25519 SSH public identity")
    authorized = f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",command="/usr/bin/false" {key}\n'
    options = {
        "AuthenticationMethods": "publickey",
        "AuthorizedKeysFile": str(home / "authorized_keys"),
        "AllowTcpForwarding": "remote",
        "PermitListen": f"127.0.0.1:{port}",
        "PermitOpen": "none",
        "AllowStreamLocalForwarding": "no",
        "GatewayPorts": "no",
        "MaxSessions": "0",
        "PermitTTY": "no",
        "AllowAgentForwarding": "no",
        "X11Forwarding": "no",
        "PermitTunnel": "no",
        "PermitUserRC": "no",
    }
    body = f"# Managed by AgentBridge for {user}\nMatch User {user}\n" + "".join(
        f"    {k} {v}\n" for k, v in options.items()
    )
    return user, home, options, body, authorized


def run_system(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True, timeout=15
    ).stdout


def secure_regular(path, expected):
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_uid == 0
        and not path.stat().st_mode & 0o022
        and path.read_text() == expected
    )


def install_fixed(path, data):
    if path.exists() or path.is_symlink():
        if not secure_regular(path, data):
            raise ValueError("Unexpected existing dedicated SSH configuration")
        return False
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o644)
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def effective(user):
    text = run_system(
        "/usr/sbin/sshd", "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"
    )
    return dict(line.split(" ", 1) for line in text.splitlines() if " " in line)


def restricted_tunnel(spec, action):
    if os.geteuid() != 0:
        raise PermissionError(
            "Tunnel enrollment requires the approved release transaction"
        )
    user, home, options, body, key = tunnel_settings(spec)
    config = Path("/etc/ssh/sshd_config.d") / (user + ".conf")
    authorized = home / "authorized_keys"
    description = "AgentBridge Pi tunnel" if spec.get("hostType") == "pi" else "AgentBridge NanoBot tunnel"
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        account = None
    if account and (
        account.pw_uid == 0
        or account.pw_dir != str(home)
        or account.pw_shell != "/usr/sbin/nologin"
        or account.pw_gecos != description
    ):
        raise ValueError("The pilot forwarding account is not publication-owned")
    if (home.exists() or home.is_symlink()) and (
        not home.is_dir()
        or home.is_symlink()
        or home.stat().st_uid != 0
        or home.stat().st_mode & 0o022
    ):
        raise ValueError("Unexpected forwarding identity directory")
    for path, expected in ((config, body), (authorized, key)):
        if (path.exists() or path.is_symlink()) and not secure_regular(path, expected):
            raise ValueError("Existing forwarding policy differs from this release")
    if action == "observe":
        return {
            "tunnelAuthorized": bool(
                account and config.exists() and authorized.exists()
            )
        }
    if action == "disable":
        authorized.unlink(missing_ok=True)
        return {"tunnelAuthorized": False}
    if action != "enable":
        raise ValueError("Unknown tunnel enrollment stage")
    baseline = {
        principal: effective(principal) for principal in ("root", "agentbridge")
    }
    if account is None:
        run_system(
            "/usr/sbin/useradd",
            "--system",
            "--no-create-home",
            "--home-dir",
            str(home),
            "--shell",
            "/usr/sbin/nologin",
            "--comment",
            description,
            user,
        )
    home.mkdir(mode=0o755, exist_ok=True)
    created = install_fixed(config, body)
    try:
        run_system("/usr/sbin/sshd", "-t")
        active = effective(user)
        if any(active.get(k.lower()) != v for k, v in options.items()) or any(
            effective(principal) != original for principal, original in baseline.items()
        ):
            raise ValueError(
                "Effective SSH policy differs from the reviewed single-port tunnel"
            )
        # An interrupted install may have written the file before reloading. No key exists until reload succeeds.
        if created or not authorized.exists():
            run_system("/usr/bin/systemctl", "reload", "ssh.service")
    except Exception:
        if created:
            config.unlink()
            run_system("/usr/sbin/sshd", "-t")
            run_system("/usr/bin/systemctl", "reload", "ssh.service")
        raise
    install_fixed(authorized, key)
    return {"tunnelAuthorized": True}
