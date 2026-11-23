"""Linux half of the optional, private NanoBot pilot. Called over the publication SSH channel."""

import fcntl
import hashlib
import json
import os
import pwd
import re
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing, contextmanager, nullcontext
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


FIRST_OPEN = "首次发布已审核的并列宿主试点"
FIRST_CLOSE = "发布验收失败，撤回首次试点开放"
RETRY_OPEN = "重新开通发布事务自动撤回的试点"
RETRY_CLOSE = "发布验收失败，撤回本次重新开通"


RENAME = "发布已审核的宿主显示名变更"
RESTORE_NAME = "撤回本次发布的宿主显示名变更"
NAME_SCOPE = ("instance_id", "host_type", "route_ref", "adapter_version", "state", "subjects")


def name_audit(event, instance):
    before = json.loads(event["before_json"] or "{}")
    after = json.loads(event["after_json"] or "{}")
    if (event["actor_username"], event["actor_role"], event["actor_account_id"],
            event["request_ip"], event["result"]) != ("native-publish", "admin", None, "local-release", "succeeded"):
        raise ValueError("Unrecognized display-name authority")
    if (event["action"] != "workspace.host.rename" or event["reason"] not in (RENAME, RESTORE_NAME)
            or set(before) != {*NAME_SCOPE, "revision", "display_name"}
            or set(after) != {*before, "candidate", "attempt"}
            or before["instance_id"] != instance or before["state"] != "enabled"
            or any(before[k] != after[k] for k in NAME_SCOPE)
            or before["display_name"] == after["display_name"] or before["revision"] + 1 != after["revision"]
            or not re.fullmatch(r"[a-f0-9]{12}", after["candidate"])
            or not re.fullmatch(r"[a-f0-9]{32}", after["attempt"])):
        raise ValueError("Invalid display-only administrator audit")
    return before, after


def display_name_action(db, spec, action, proof, audit=None):
    from agentbridge.workspace.host_instances import WorkspaceHostInstances

    instance = spec["instanceId"]
    current = WorkspaceHostInstances._admin_row(db, instance)
    migration = spec["displayNameChange"]
    source, target = migration["from"], spec["displayName"]
    if action == "name_restore" and not db.execute(
        "SELECT 1 FROM admin_audit_events WHERE target_type='workspace_host' AND target_id=? "
        "AND action='workspace.host.rename' AND json_extract(after_json,'$.attempt')=?",
        (instance, proof["attempt"]),
    ).fetchone():
        return {"changed": False}
    if (current["subjects"], current["route_ref"], current["adapter_version"], current["host_type"], current["state"]) != (
            sorted(spec["subjects"]), spec["routeRef"], spec["adapterVersion"], "nanobot", "enabled"):
        raise ValueError("Display-name change has no authority over host assignments")
    revision = current["revision"]
    if action == "name_check":
        if current["display_name"] == target:
            return {"matches": True, "revision": revision}
        if current["display_name"] != source or revision < migration["expectedRevision"]:
            raise ValueError("The previous approved host display name or revision differs")
        # A retried upgrade may follow our own successful name compensation. Any
        # other intervening administrative edit needs a newly reviewed profile.
        changes = db.execute("SELECT * FROM admin_audit_events WHERE target_type='workspace_host' AND target_id=? "
                             "AND action='workspace.host.rename'", (instance,)).fetchall()
        names = {end["revision"]: (event["reason"], begin["display_name"], end["display_name"])
                 for event in changes for begin, end in (name_audit(event, instance),)}
        previous = source
        for number in range(migration["expectedRevision"] + 1, revision + 1):
            reason, was, now = names.get(number, (None, None, None))
            if was != previous or (reason, was, now) not in ((RENAME, source, target), (RESTORE_NAME, target, source)):
                raise ValueError("Intervening administrator edit prevents display-name retry")
            previous = now
        if previous != source:
            raise ValueError("Original approved name not restored")
        return {"matches": False, "revision": revision}
    if (not re.fullmatch(r"[a-f0-9]{12}", proof["candidate"]) or not re.fullmatch(r"[a-f0-9]{32}", proof["attempt"])
            or type(proof["revision"]) is not int or proof["revision"] < migration["expectedRevision"]):
        raise ValueError("Invalid display-name release proof")
    events = db.execute("SELECT * FROM admin_audit_events WHERE target_type='workspace_host' AND target_id=? "
                        "AND action='workspace.host.rename' AND json_extract(after_json,'$.attempt')=? ORDER BY rowid",
                        (instance, proof["attempt"])).fetchall()
    for event in events:
        begin, end = name_audit(event, instance)
        if end["candidate"] != proof["candidate"] or begin["revision"] < proof["revision"]:
            raise ValueError("Different release owns the display-name receipt")
    reverse = action == "name_restore"
    if not reverse and action != "name_apply":
        raise ValueError("Invalid display-name release action")
    if events and events[-1]["reason"] == (RESTORE_NAME if reverse else RENAME):
        end = name_audit(events[-1], instance)[1]
        if revision == end["revision"] and current["display_name"] == end["display_name"]:
            return {"changed": True}
        raise ValueError("A later administrator edit superseded the host display name")
    old, new, reason = (target, source, RESTORE_NAME) if reverse else (source, target, RENAME)
    if revision != proof["revision"] + int(reverse) or current["display_name"] != old:
        raise ValueError("Administrator changed the reviewed name or revision")
    if db.execute("UPDATE workspace_host_instances SET display_name=?, revision=revision+1 WHERE instance_id=? "
                  "AND revision=? AND display_name=?", (new, instance, revision, old)).rowcount != 1:
        raise ValueError("Display-name update lost the expected revision")
    db.execute("INSERT INTO workspace_host_directory_audit(instance_id,revision,actor,reason) VALUES(?,?,?,?)",
               (instance, revision + 1, "native-publish", reason))
    before = {k: current[k] for k in (*NAME_SCOPE, "display_name", "revision")}
    audit.append(actor={"username":"native-publish","role":"admin"}, request_ip="local-release",
                 action="workspace.host.rename", target_type="workspace_host", target_id=instance, result="succeeded",
                 reason=reason, before=before, connection=db, after={**before, "display_name":new,
                     "revision":revision + 1, "candidate":proof["candidate"], "attempt":proof["attempt"]})
    return {"changed": True}


def recovery_history(db, spec, current):
    """Match directory revisions to their independent immutable administrator audit."""
    instance = spec["instanceId"]
    audit = db.execute(
        "SELECT * FROM admin_audit_events WHERE target_type='workspace_host' AND target_id=?",
        (instance,),
    ).fetchall()
    revisions = {}
    names = {}
    bindings = {}
    for event in audit:
        if (
            event["actor_username"],
            event["actor_role"],
            event["actor_account_id"],
            event["request_ip"],
            event["result"],
        ) != ("native-publish", "admin", None, "local-release", "succeeded"):
            raise ValueError("Pilot administrator history changed")
        detail = json.loads(event["after_json"] or "{}")
        if event["action"] == "workspace.host.bind":
            bindings.setdefault(detail.get("user_subject"), set()).add(
                detail.get("endpoint_id")
            )
        elif event["action"] == "workspace.host.rename":
            _, rename = name_audit(event, instance)
            if rename["revision"] in names:
                raise ValueError("Duplicate native name audit")
            names[rename["revision"]] = event["reason"]
        elif event["action"] == "workspace.host.configure":
            revision = detail["revision"]
            if revision in revisions:
                raise ValueError("Duplicate pilot configuration audit")
            revisions[revision] = (event["reason"], detail, event["created_at"])
        else:
            raise ValueError("Unexpected pilot administrator action")
    rows = db.execute(
        "SELECT revision,actor,reason FROM workspace_host_directory_audit WHERE instance_id=? ORDER BY revision",
        (instance,),
    ).fetchall()
    expected = list(range(1, current["revision"] + 1))
    if (
        sorted([*revisions, *names]) != expected
        or [r["revision"] for r in rows] != expected
        or len(rows) < 2
    ):
        raise ValueError("Incomplete native rollback history")
    authority = {}
    for row in rows:
        physical = row["revision"]
        if physical in names:
            if (row["actor"], row["reason"]) != ("native-publish", names[physical]):
                raise ValueError("Display-only audit differs from directory history")
            continue
        revision = len(authority) + 1
        authority[revision] = revisions[physical]
        reason, detail, _ = authority[revision]
        enabled = revision % 2 == 1
        required = (
            (FIRST_OPEN if enabled else FIRST_CLOSE)
            if revision <= 2
            else (RETRY_OPEN if enabled else RETRY_CLOSE)
        )
        if (row["actor"], row["reason"], reason, detail["state"]) != (
            "native-publish",
            required,
            required,
            "enabled" if enabled else "revoked",
        ):
            raise ValueError("Scope was changed outside the native rollback")
        if revision > 2 and not (
            detail.get("attempt") and detail.get("candidate") and detail.get("tokenIds")
        ):
            raise ValueError("Missing native transaction receipt")
    if len(authority) < 2 or current["state"] != authority[len(authority)][1]["state"]:
        raise ValueError("Pilot state differs from recorded revision")
    return authority, bindings


def credential_file(path, uid):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Unexpected native credential ownership")
    return path.read_bytes()


def recovery_personal(
    db,
    provision,
    spec,
    accounts,
    config,
    uid,
    current,
    history,
    proof,
    base,
    subject,
    ref,
):
    revisions, bound = history
    revision = len(revisions)
    instance = spec["instanceId"]
    supplied = proof["subjects"][subject]
    binding = provision.directory._binding(
        db, provision.directory._account(db, accounts[subject]), current
    )
    fields = (
        "user_subject",
        "endpoint_id",
        "endpoint_key",
        "session_key",
        "credential_ref",
    )
    if (
        not binding
        or any(binding[k] != supplied.get(k) for k in fields)
        or binding["credential_ref"] != ref
    ):
        raise ValueError("Personal endpoint differs from the archived host")
    if bound.get(subject) != {binding["endpoint_id"]} or set(bound) != set(accounts):
        raise ValueError(
            "Personal endpoint differs from the original administrator audit"
        )
    label = f"workspace-nanobot:{instance}:{ref}"
    rows = db.execute(
        "SELECT * FROM mcp_identity_tokens WHERE label=? ORDER BY created_at", (label,)
    ).fetchall()
    if len(rows) != (revision + 1) // 2:
        raise ValueError("Unexpected personal credential generation")
    by_id = {r["token_id"]: r for r in rows}
    original = rows[0]
    original_hash = hashlib.sha256(
        credential_file(config / (ref + ".token"), uid)
    ).hexdigest()
    if original_hash != original["token_hash"]:
        raise ValueError("Original central credential differs from the issued identity")
    for index, token in enumerate(rows):
        opened, closed = 1 + index * 2, 2 + index * 2
        active = closed > revision
        if (token["user_subject"], token["state"], token["edit_revision"]) != (
            subject,
            "active" if active else "revoked",
            0 if active else 1,
        ) or db.execute(
            "SELECT 1 FROM admin_audit_events WHERE target_type='mcp_token' AND target_id=?",
            (token["token_id"],),
        ).fetchone():
            raise ValueError(
                "Personal credential was changed independently of publication"
            )
        if index and revisions[opened][1]["tokenIds"].get(ref) != token["token_id"]:
            raise ValueError(
                "Issued credential differs from the native transaction receipt"
            )
        if (
            index
            and not active
            and revisions[closed][1]["tokenIds"].get(ref) != token["token_id"]
        ):
            raise ValueError(
                "Revoked credential differs from the native transaction receipt"
            )
        if not index and datetime.fromisoformat(
            token["revoked_at"]
        ) < datetime.fromisoformat(revisions[2][2]):
            raise ValueError(
                "Original personal credential predates the automatic rollback"
            )
    retired = [r["token_hash"] for r in rows[: base // 2]]
    if supplied.get("fileDigest") not in retired:
        raise ValueError("The Mac credential is not an exact retired generation")
    principals = {
        r[0]
        for r in db.execute(
            "SELECT expected_principal_ref FROM mcp_identity_tokens WHERE user_subject=? AND state='active' "
            "AND expires_at>? AND (label IS NULL OR label<>?)",
            (subject, datetime.now(timezone.utc).isoformat(), label),
        )
    }
    if principals != {original["expected_principal_ref"]}:
        raise ValueError(
            "Personal subject authority changed since the original admission"
        )
    return (
        binding,
        label,
        original["expected_principal_ref"],
        supplied["fileDigest"],
        rows[-1]["token_id"],
        by_id,
    )


def recover_scope(request, provision, actor, accounts, config, uid, gid):
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from agentbridge.core.store_transactions import store_transaction

    # Keep issuance, directory revision and both token revocations in one SQLite transaction.
    class TransactionIdentities(McpIdentityTokenStore):
        def __init__(self, db):
            self.connection = db
            self.clock = lambda: datetime.now(timezone.utc)

        def _connect(self):
            return nullcontext(self.connection)

    spec, proof, action = request["spec"], request["recovery"], request["action"]
    instance = spec["instanceId"]
    if not (
        re.fullmatch(r"[a-f0-9]{12}", proof["sourceCandidate"])
        and re.fullmatch(r"[a-f0-9]{12}", proof["candidate"])
        and re.fullmatch(r"[a-f0-9]{32}", proof["attempt"])
        and set(proof["subjects"]) == set(accounts) == set(spec["subjects"])
    ):
        raise ValueError("Invalid native transaction proof")
    if action not in {"recovery_check", "bind", "rollback_scope"}:
        raise ValueError("Invalid recovery action")
    new_files = []
    try:
        with store_transaction(provision.service.tasks) as db:
            current = provision.directory._admin_row(db, instance)
            if (
                current["subjects"],
                current["route_ref"],
                current["adapter_version"],
                current["host_type"],
                current["display_name"],
            ) != (
                sorted(accounts),
                spec["routeRef"],
                spec["adapterVersion"],
                "nanobot",
                spec["displayName"],
            ):
                raise ValueError(
                    "The rolled-back scope differs from the reviewed profile"
                )
            revisions, bound = recovery_history(db, spec, current)
            revision = len(revisions)
            latest = revisions[revision][1]
            own = (
                latest.get("attempt") == proof["attempt"]
                and latest.get("candidate") == proof["candidate"]
            )
            base = (
                revision
                if not own
                else revision - (1 if current["state"] == "enabled" else 2)
            )
            if (
                base < 2
                or base % 2
                or (
                    base > 2
                    and (
                        revisions[base][1].get("attempt"),
                        revisions[base][1].get("candidate"),
                    )
                    != (proof.get("previousAttempt"), proof["sourceCandidate"])
                )
                or ("baseRevision" in proof and base != proof["baseRevision"])
            ):
                raise ValueError(
                    "The previous publication rollback is no longer current"
                )
            if action != "recovery_check" and proof.get("baseRevision") != base:
                raise ValueError("The native rollback preflight is missing")
            if current["state"] == "enabled" and not own:
                raise ValueError("Another transaction owns the pilot")
            if own and current["state"] == "revoked" and action != "rollback_scope":
                raise ValueError("The native retry already rolled back")
            identities = TransactionIdentities(db)
            evidence, records, subject_rows = {}, [], {}
            for subject, ref in spec["subjects"].items():
                personal = recovery_personal(
                    db,
                    provision,
                    spec,
                    accounts,
                    config,
                    uid,
                    current,
                    (revisions, bound),
                    proof,
                    base,
                    subject,
                    ref,
                )
                evidence[ref] = personal[4]
                subject_rows[ref] = personal
            if action == "recovery_check":
                return {"baseRevision": base}
            if action == "rollback_scope" and (
                not own or current["state"] == "revoked"
            ):
                return {"closed": True, "reverted": own}
            issue = action == "bind" and not own
            for ref, (
                binding,
                label,
                principal,
                retired_digest,
                _,
                by_id,
            ) in subject_rows.items():
                if action == "rollback_scope":
                    identities.revoke(evidence[ref])
                    continue
                if issue:
                    issued = identities.issue(
                        user_subject=binding["user_subject"],
                        expected_principal_ref=principal,
                        label=label,
                        ttl_seconds=30 * 86400,
                    )
                    evidence[ref] = issued["token_id"]
                    path = config / ("issued-" + issued["token_id"] + ".token")
                    store_private(path, issued["token"].encode(), uid, gid)
                    new_files.append(path)
                    secret = issued["token"]
                else:
                    token = by_id[evidence[ref]]
                    raw = credential_file(
                        config / ("issued-" + token["token_id"] + ".token"), uid
                    )
                    if hashlib.sha256(raw).hexdigest() != token["token_hash"]:
                        raise ValueError("Native credential generation changed")
                    secret = raw.decode()
                public = {
                    k: v
                    for k, v in binding.items()
                    if k not in {"host_type", "route_ref"}
                }
                records.append(
                    {
                        "binding": public,
                        "personalCredential": secret,
                        "retiredCredentialDigest": retired_digest,
                    }
                )
            if issue or action == "rollback_scope":
                state, reason = (
                    ("enabled", RETRY_OPEN) if issue else ("revoked", RETRY_CLOSE)
                )
                changed = provision.directory.save_instance(
                    instance,
                    display_name=current["display_name"],
                    host_type="nanobot",
                    route_ref=current["route_ref"],
                    adapter_version=current["adapter_version"],
                    state=state,
                    subjects=current["subjects"],
                    expected_revision=current["revision"],
                    actor=actor["username"],
                    reason=reason,
                    connection=db,
                )
                provision.audit.append(
                    actor=actor,
                    request_ip="local-release",
                    action="workspace.host.configure",
                    target_type="workspace_host",
                    target_id=instance,
                    reason=reason,
                    result="succeeded",
                    connection=db,
                    after={
                        "state": state,
                        "revision": changed["revision"],
                        "attempt": proof["attempt"],
                        "candidate": proof["candidate"],
                        "sourceCandidate": proof["sourceCandidate"],
                        "tokenIds": evidence,
                    },
                )
        return (
            {"closed": True, "reverted": True}
            if action == "rollback_scope"
            else {"bindings": records}
        )
    except BaseException:
        for path in new_files:
            path.unlink(missing_ok=True)
        raise


@contextmanager
def admission_lock(config):
    fd = os.open(
        config / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


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
    if action == "name_check":
        with closing(sqlite3.connect(encoded, uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return display_name_action(db, spec, action, {})
    if action in ("name_apply", "name_restore"):
        from agentbridge.admin.stores import AdminAuditStore
        with closing(sqlite3.connect(db_path, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            audit = AdminAuditStore(db_path)
            with db:
                db.execute("BEGIN IMMEDIATE")
                return display_name_action(db, spec, action, request["nameProof"], audit)
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
    if "recovery" in request:
        with admission_lock(config):
            return recover_scope(
                request, provision, actor, accounts, config, user.pw_uid, user.pw_gid
            )
    if action == "rollback_scope":
        if current and current["state"] == "revoked":
            return {"closed": True}
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
    with admission_lock(config):
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
        # A recovered instance keeps the original revoked file as evidence; the
        # immutable activation event names the private file of the active generation.
        with closing(sqlite3.connect(encoded, uri=True)) as db:
            issued_event = db.execute(
                "SELECT after_json FROM admin_audit_events WHERE target_type='workspace_host' AND target_id=? "
                "AND action='workspace.host.configure' AND actor_username='native-publish' AND reason=? "
                "ORDER BY rowid DESC LIMIT 1",
                (instance, RETRY_OPEN),
            ).fetchone()
        issued_generations = (
            json.loads(issued_event[0])["tokenIds"] if issued_event else {}
        )
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
            if ref in issued_generations:
                path = config / ("issued-" + issued_generations[ref] + ".token")
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
