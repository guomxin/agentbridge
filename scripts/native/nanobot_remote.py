"""Linux half of the optional, private NanoBot pilot. Called over the publication SSH channel."""

import fcntl
import json
import os
import pwd
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from nanobot_tunnel import restricted_tunnel


def store_private(path, data, uid, gid):
    if path.is_symlink():
        raise ValueError("Unexpected credential symlink")
    if path.exists():
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Private route material has unexpected ownership")
        if path.read_bytes() != data:
            raise ValueError(
                "Existing private material differs; explicit rotation required"
            )
        return
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchown(f.fileno(), uid, gid)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def account_snapshot(db, subjects):
    rows = db.execute(
        "SELECT account_id,user_subject,endpoint_id FROM workspace_accounts "
        "WHERE state='active'"
    ).fetchall()
    accounts = {r[1]: r[0] for r in rows if r[1] in subjects and r[2]}
    if set(accounts) != set(subjects):
        raise ValueError(
            "Pilot subjects must already have distinct active Workspace accounts"
        )
    return accounts


def execute(request):
    action = request["action"]
    root = Path(request["root"])
    spec = request["spec"]
    instance = spec["instanceId"]
    subjects = spec["subjects"]
    user = pwd.getpwnam("agentbridge")
    db_path = root / "data/agentbridge.db"
    encoded = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(encoded, uri=True)) as db:
        accounts = account_snapshot(db, subjects)
        scope_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workspace_host_instances'"
        ).fetchone()
        present = bool(
            scope_table
            and db.execute(
                "SELECT 1 FROM workspace_host_instances WHERE instance_id=?",
                (instance,),
            ).fetchone()
        )
        preference_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workspace_host_preferences'"
        ).fetchone()

        def defaults(source):
            return sorted(
                source.execute(
                    "SELECT account_id,instance_id,revision FROM workspace_host_preferences "
                    "WHERE account_id IN (?,?)",
                    tuple(accounts.values()),
                ).fetchall()
            )

        baseline = defaults(db) if preference_table else None
    config = root / "config" / ("nanobot-" + instance)
    routes = config / "routes.json"
    if action in ("tunnel_enable", "tunnel_disable"):
        return restricted_tunnel(spec, action.removeprefix("tunnel_"))
    if action == "observe":
        ports = {spec["remotePort"]}
        occupied = any(
            int(line.split()[1].split(":")[1], 16) in ports and line.split()[3] == "0A"
            for name in ("tcp", "tcp6")
            for line in Path("/proc/net/" + name).read_text().splitlines()[1:]
        )
        return {
            **restricted_tunnel(spec, "observe"),
            "subjects": len(accounts),
            "routeInstalled": routes.is_file(),
            "scopePresent": present,
            "portOccupied": occupied,
            "productionActions": False,
        }
    if action == "stage":
        if os.geteuid() != 0:
            raise PermissionError(
                "Publication privilege required to stage the controlled route"
            )
        config.mkdir(mode=0o700, exist_ok=True)
        if config.is_symlink() or config.parent.is_symlink():
            raise ValueError("Unexpected route directory")
        info = config.stat()
        if info.st_uid not in {user.pw_uid, 0} or info.st_mode & 0o077:
            raise ValueError("Unexpected route directory ownership")
        os.chown(config, user.pw_uid, user.pw_gid)
        store_private(
            config / "service.token",
            request["serviceSecret"].encode(),
            user.pw_uid,
            user.pw_gid,
        )
        row = {
            "url": "http://127.0.0.1:" + str(spec["remotePort"]),
            "instanceId": instance,
            "tokenFile": str(config / "service.token"),
            "adapterVersion": spec["adapterVersion"],
            "subjects": subjects,
        }
        if spec.get("telegram"):
            row["telegram"] = spec["telegram"]
        data = json.dumps({"routes": {spec["routeRef"]: row}}, sort_keys=True).encode()
        store_private(routes, data, user.pw_uid, user.pw_gid)
        return {"staged": True}
    if os.geteuid() != user.pw_uid:
        raise PermissionError("Central provisioning runs under the service identity")
    os.environ["AGENTBRIDGE_NANOBOT_ROUTES_FILE"] = str(routes)
    from types import SimpleNamespace

    from agentbridge.admin.stores import AdminAuditStore
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from agentbridge.core.tasks import TaskHubStore
    from agentbridge.workspace.host_provision import WorkspaceHostProvision
    from agentbridge.workspace.stores import WorkspaceStore

    service = SimpleNamespace(
        db_path=db_path,
        workspace=WorkspaceStore(db_path),
        tasks=TaskHubStore(db_path, maintain_on_startup=False),
    )
    provision = WorkspaceHostProvision(service, AdminAuditStore(db_path), None)
    actor = {"username": "native-publish", "role": "admin"}
    current = next(
        (r for r in provision.directory.admin_list() if r["instance_id"] == instance),
        None,
    )
    if action == "rollback_scope":
        with closing(sqlite3.connect(encoded, uri=True)) as db:
            creator = db.execute(
                "SELECT actor FROM workspace_host_directory_audit WHERE instance_id=? AND revision=1",
                (instance,),
            ).fetchone()
        if (
            current
            and current["revision"] == 1
            and current["route_ref"] == spec["routeRef"]
            and creator == (actor["username"],)
        ):
            provision.save(
                actor,
                "local-release",
                {
                    "instance_id": instance,
                    "display_name": current["display_name"],
                    "route_ref": current["route_ref"],
                    "state": "revoked",
                    "subjects": current["subjects"],
                    "expected_revision": 1,
                    "reason": "发布验收失败，撤回首次试点开放",
                },
            )
        elif current and current["state"] != "revoked":
            raise RuntimeError(
                "An administrator changed the pilot after release. Review its scope manually"
            )
        identities = McpIdentityTokenStore(db_path)
        for subject, ref in subjects.items():
            for identity in identities.list(user_subject=subject, limit=1000):
                if (
                    identity["label"] == f"workspace-nanobot:{instance}:{ref}"
                    and identity["state"] == "active"
                ):
                    identities.revoke(identity["token_id"])
        return {"closed": True}
    if action == "probe":
        client = provision._routes()[spec["routeRef"]]["client"]
        result = client.readiness()
        required = {"chat", "credential_card", "attachments"}
        if spec.get("requiredLevel") == "L3":
            required.update(("skills", "skill_trial", "skill_model"))
        if (
            not result["available"]
            or result.get("adapterVersion") != spec["adapterVersion"]
            or not required.issubset(result.get("capabilities", ()))
        ):
            raise RuntimeError(
                "Deployed Workspace route failed authenticated readiness"
            )
        return {
            "ready": True,
            "subjects": len(accounts),
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }
    if action != "bind":
        raise ValueError("Unknown deployment stage")
    lock_fd = os.open(
        config / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = next(
            (
                r
                for r in provision.directory.admin_list()
                if r["instance_id"] == instance
            ),
            None,
        )
        if current is None:
            current = provision.save(
                actor,
                "local-release",
                {
                    "instance_id": instance,
                    "display_name": spec["displayName"],
                    "route_ref": spec["routeRef"],
                    "state": "enabled",
                    "subjects": sorted(subjects),
                    "expected_revision": 0,
                    "reason": "首次发布已审核的并列宿主试点",
                },
            )
        if (
            current["state"] != "enabled"
            or current["route_ref"] != spec["routeRef"]
            or current["subjects"] != sorted(subjects)
            or current["adapter_version"] != spec["adapterVersion"]
        ):
            raise ValueError(
                "Current administrator scope/state/version differs. Publication cannot override an administrator's decision."
            )
        identities = McpIdentityTokenStore(db_path)
        result = []
        for subject, account_id in accounts.items():
            ref = subjects[subject]
            binding = provision.bind(
                actor,
                "local-release",
                {
                    "instance_id": instance,
                    "account_id": account_id,
                    "expected_revision": current["revision"],
                    "reason": "开通经审核的试点主体，保留个人默认设置",
                },
            )
            path = config / (ref + ".token")
            label = "workspace-nanobot:" + instance + ":" + ref
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_uid == user.pw_uid
                and not path.stat().st_mode & 0o077
            ):
                secret = path.read_text()
                record = identities.verify(secret)
                if (
                    not record
                    or record["user_subject"] != subject
                    or record["label"] != label
                ):
                    raise PermissionError(
                        "Existing pilot credential needs an administrator review"
                    )
            else:
                records = identities.list(user_subject=subject, limit=1000)
                if any(r["label"] == label for r in records):
                    raise ValueError(
                        "Lost private material for an issued pilot identity; administrative recovery required"
                    )
                principals = {
                    r["expected_principal_ref"]
                    for r in records
                    if r["state"] == "active"
                    and datetime.fromisoformat(r["expires_at"])
                    > datetime.now(timezone.utc)
                }
                if len(principals) != 1:
                    raise ValueError(
                        "A previously verified central identity is required for each pilot subject"
                    )
                issued = identities.issue(
                    user_subject=subject,
                    expected_principal_ref=principals.pop(),
                    label=label,
                    ttl_seconds=30 * 86400,
                )
                secret = issued["token"]
                try:
                    store_private(path, secret.encode(), user.pw_uid, user.pw_gid)
                except Exception:
                    identities.revoke(issued["token_id"])
                    raise
            result.append({"binding": binding, "personalCredential": secret})
        # This response travels only in an authenticated SSH pipe to mode-0600 files on the host.
        with closing(sqlite3.connect(encoded, uri=True)) as db:
            if baseline is not None and baseline != defaults(db):
                raise RuntimeError(
                    "A user default changed during provisioning; review without overwriting it"
                )
        return {"bindings": result}


if __name__ == "__main__":
    print(json.dumps(execute(json.loads(sys.stdin.readline()))))
