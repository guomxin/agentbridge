"""Reviewed, optional Mac NanoBot instance deployed by the main publication transaction."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from common import ROOT, external, private_file, read, run, save, sha
from nanobot_tunnel import tunnel_settings, tunnel_user

ALIAS = re.compile(r"[a-z][a-z0-9_.-]{0,64}\Z")
BRIDGE = Path("integrations/nanobot-agentbridge/nanobot_agentbridge")
COORDINATOR = Path("integrations/reference-host/reference_host")


def owned_directory(path):
    root = external(path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        root != Path(path)
        or root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise ValueError("Dedicated NanoBot directory must be a private absolute path")
    return root


def checked_profile(path, environment, host_profile, units, remote_root):
    path = private_file(path)
    reference = read(private_file(environment)).get("nanoBotPilot", {})
    if reference != {"path": str(path), "sha256": sha(path.read_bytes())}:
        raise ValueError("Environment profile must pin the private NanoBot profile")
    spec = read(path)
    flags = {
        "schema",
        "instanceId",
        "routeRef",
        "displayName",
        "adapterVersion",
        "enginePython",
        "runtimeDir",
        "listenPort",
        "remotePort",
        "subjects",
        "modelConfigFile",
        "modelOAuthTokenFile",
        "serviceTokenFile",
        "macHostSha256",
        "centralMcpUrl",
        "trustedCardOrigins",
        "tunnelIdentityFile",
        "tunnelPublicKey",
    }
    if (
        set(spec) - {"telegram", "coordinatorPython", "requiredLevel"} != flags
        or spec["schema"] != "agentbridge.nanobot-mac-pilot.v1"
    ):
        raise ValueError("Unexpected NanoBot pilot configuration fields")
    if not ALIAS.fullmatch(spec["instanceId"]) or not ALIAS.fullmatch(spec["routeRef"]):
        raise ValueError("Invalid pilot identifiers")
    subjects = spec["subjects"]
    if (
        not isinstance(subjects, dict)
        or len(subjects) != 2
        or len(set(subjects.values())) != 2
        or any(
            not isinstance(s, str)
            or not s.strip()
            or len(s) > 256
            or not s.isprintable()
            for s in subjects
        )
        or any(not ALIAS.fullmatch(ref) for ref in subjects.values())
    ):
        raise ValueError(
            "A pilot needs two explicit central subjects and distinct credential references"
        )
    if not host_profile or spec["macHostSha256"] != sha(
        private_file(host_profile).read_bytes()
    ):
        raise ValueError("The independent host must be bound to this reviewed Mac")
    mac = read(private_file(host_profile))
    tunnel_settings(spec)
    tunnel_key = private_file(spec["tunnelIdentityFile"])
    if (
        tunnel_key.parent != Path(spec["runtimeDir"]) / "tunnel"
        or tunnel_key.samefile(mac["tunnel"]["identityFile"])
        or run(["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", tunnel_key]).split()[:2]
        != spec["tunnelPublicKey"].split()
    ):
        raise ValueError("A dedicated NanoBot forwarding identity is required")
    if (
        type(spec["listenPort"]) is not int
        or type(spec["remotePort"]) is not int
        or min(spec["listenPort"], spec["remotePort"]) < 1024
        or max(spec["listenPort"], spec["remotePort"]) > 65535
        or spec["listenPort"] == mac["port"]
        or spec["remotePort"] == mac["tunnel"]["remotePort"]
    ):
        raise ValueError(
            "NanoBot requires an independent high loopback port at each end"
        )
    from urllib.parse import urlsplit

    mcp = urlsplit(spec["centralMcpUrl"])
    if (
        mcp.scheme != "https"
        or mcp.hostname != mac["tunnel"]["host"]
        or mcp.path != "/mcp"
        or mcp.username
        or mcp.password
        or mcp.query
        or mcp.fragment
    ):
        raise ValueError("Expected the reviewed central TLS MCP origin")
    for origin in spec["trustedCardOrigins"]:
        card = urlsplit(origin)
        if (
            card.scheme != "https"
            or card.hostname != mcp.hostname
            or origin != "https://" + card.netloc
        ):
            raise ValueError("Unexpected trusted card origin")
    root = external(spec["runtimeDir"])
    if (
        root != Path(spec["runtimeDir"])
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise ValueError("Required private NanoBot runtime directory is unavailable")
    for key in ("serviceTokenFile", "modelOAuthTokenFile", "modelConfigFile"):
        if not private_file(spec[key]).is_relative_to(root):
            raise ValueError(
                "Pilot credentials and model configuration must belong to the dedicated host"
            )
    engine = Path(spec["enginePython"]).parent.parent
    if not engine.is_relative_to(root) or not (engine / "pyvenv.cfg").is_file():
        raise ValueError(
            "A dedicated production NanoBot venv must live under the instance runtime"
        )
    if spec.get("telegram"):
        pilot = spec["telegram"]
        ref = pilot.get("botCredential", {})
        if (
            set(pilot) != {"botId", "username", "botCredential"}
            or type(pilot["botId"]) != int
            or not 0 < pilot["botId"] < 2**53
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", pilot["username"])
            or set(ref) != {"kind", "service", "account"}
            or ref["kind"] != "macos-keychain"
        ):
            raise ValueError(
                "An independent Telegram pilot needs a reviewed public bot and Keychain reference"
            )
    version = run(
        [
            spec["enginePython"],
            "-I",
            "-c",
            "import importlib.metadata as m;print(m.version('nanobot-ai'))",
        ]
    )
    level = spec.get("requiredLevel", "L2")
    if level not in {"L2", "L3"} or (level == "L3") != bool(
        spec.get("coordinatorPython")
    ):
        raise ValueError("The L3 profile requires its independent coordinator runtime")
    if level == "L3":
        coord = Path(spec["coordinatorPython"])
        if (
            not coord.parent.parent.is_relative_to(root)
            or not (coord.parent.parent / "pyvenv.cfg").is_file()
        ):
            raise ValueError(
                "Coordinator requires an independent reviewed runtime under the pilot"
            )
        if (
            run(
                [
                    coord,
                    "-I",
                    "-c",
                    "import importlib.metadata as m;print(m.version('mcp'))",
                ]
            )
            != "1.23.2"
        ):
            raise ValueError("The reference coordinator requires MCP 1.23.2")
    if version != "0.4.2":
        raise ValueError("Expected the reviewed NanoBot 0.4.2 runtime")
    config = read(private_file(spec["modelConfigFile"]))
    defaults = config.get("agents", {}).get("defaults", {})
    if (
        defaults.get("provider") != "openai_codex"
        or defaults.get("model") != "openai-codex/gpt-5.6-sol"
    ):
        raise ValueError("Pilot model configuration differs from reviewed OAuth canary")
    remote_file = remote_root + "/config/nanobot-" + spec["instanceId"] + "/routes.json"
    expected = "Environment=AGENTBRIDGE_NANOBOT_ROUTES_FILE=" + remote_file
    if expected not in units["agentbridge.service"].decode().splitlines():
        raise ValueError(
            "The reviewed central unit must load this exact private route file"
        )
    return spec


def metadata(spec):
    result = {
        k: spec[k]
        for k in (
            "instanceId",
            "routeRef",
            "displayName",
            "adapterVersion",
            "remotePort",
            "subjects",
            "tunnelPublicKey",
        )
    }
    result["requiredLevel"] = spec.get("requiredLevel", "L2")
    if spec.get("telegram"):
        result["telegram"] = {k: spec["telegram"][k] for k in ("botId", "username")}
    return result


def reviewed_contract(spec):
    from agentbridge.core.host_contract import HostContractStore

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "compatibility.sqlite3"
        HostContractStore(path)
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT accepted_level,evidence_digest FROM agent_host_compatibility "
                "WHERE implementation_name='nanobot' AND implementation_version=?",
                (spec["adapterVersion"],),
            ).fetchone()
        required = spec.get("requiredLevel", "L2")
        return {
            "required": required,
            "approved": bool(row and row[0] == required and row[1]),
            "evidence": row[1] if row else None,
        }


def remote(pub, spec, action, **extra):
    # The script and any credentials pass on stdin. No literal secret in ssh/ps arguments.
    tunnel_code = (ROOT / "scripts/native/nanobot_tunnel.py").read_bytes()
    program = (
        "import base64,types,sys\nt=types.ModuleType('nanobot_tunnel')\n"
        "exec(base64.b64decode("
        + repr(base64.b64encode(tunnel_code))
        + "),t.__dict__)\n"
        "sys.modules['nanobot_tunnel']=t\n"
        + (ROOT / "scripts/native/nanobot_remote.py").read_text()
    )
    runner = "import base64,sys;exec(compile(base64.b64decode(sys.stdin.readline()),'<reviewed-nanobot-release>','exec'))"
    if action in ("bind", "probe", "rollback_scope", "recovery_check"):
        args = [
            "sudo",
            "-u",
            "agentbridge",
            "--",
            pub.args.remote_root + "/current/venv/bin/python",
            "-I",
            "-c",
            runner,
        ]
    else:
        args = ["python3", "-I", "-c", runner]
    request = {
        "action": action,
        "spec": metadata(spec),
        "root": pub.args.remote_root,
        **extra,
    }
    data = (
        base64.b64encode(program.encode()).decode() + "\n" + json.dumps(request) + "\n"
    )
    return json.loads(pub.ssh(shlex.join(args), input=data, timeout=40))


def profile_for(pub):
    ref = read(external(pub.args.profile)).get("nanoBotPilot")
    requested = getattr(pub.args, "nanobot_profile", None)
    if not ref and not requested:
        return None
    if not isinstance(ref, dict) or requested and requested != ref.get("path"):
        raise ValueError("Explicit pilot profile differs from reviewed environment")
    return checked_profile(
        ref["path"],
        pub.args.profile,
        pub.args.host_profile,
        pub.units,
        pub.args.remote_root,
    )


def observability(pub, spec, *, offline):
    return {
        "compatibility": reviewed_contract(spec),
        "instance": spec["instanceId"],
        "subjects": len(spec["subjects"]),
        "adapterVersion": spec["adapterVersion"],
        "remote": None if offline else remote(pub, spec, "observe"),
    }


def archive_bridge(pub, spec, artifact):
    import tomllib

    root = owned_directory(spec["runtimeDir"])
    release = root / "releases" / pub.release
    sources = [BRIDGE] + ([COORDINATOR] if spec.get("coordinatorPython") else [])
    expected = {}
    for package_root in sources:
        files = run(
            ["git", "ls-tree", "-r", "--name-only", pub.commit, "--", str(package_root)]
        ).splitlines()
        for source in files:
            rel = Path(source).relative_to(package_root)
            if package_root == COORDINATOR and rel.parts[0] == "static":
                continue
            if rel.suffix != ".py" or len(rel.parts) != 1:
                raise ValueError("Unexpected host source in release archive")
            blob = subprocess.run(
                ["git", "show", pub.commit + ":" + source],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            expected[Path(package_root.name) / rel] = blob
    version = tomllib.loads(
        run(
            [
                "git",
                "show",
                pub.commit + ":integrations/nanobot-agentbridge/pyproject.toml",
            ]
        )
    )["project"]["version"]
    if (
        Path("nanobot_agentbridge/server.py") not in expected
        or version != spec["adapterVersion"]
    ):
        raise ValueError("Pilot adapter release/version mismatch")
    if spec.get("coordinatorPython"):
        program = (
            "import sys;sys.path.insert(0,"
            + repr(str(release))
            + ");from reference_host.bound_coordinator import main;main()"
        )
        expected[Path("coordinator")] = (
            "#!/bin/sh\nexec "
            + shlex.join([spec["coordinatorPython"], "-I", "-B", "-c", program])
            + "\n"
        ).encode()
    package = release / "nanobot_agentbridge"
    wheel = Path(artifact["wheel"])
    if not wheel.is_file() or sha(wheel.read_bytes()) != artifact["sha256"]:
        raise ValueError(
            "Full receipt does not match the central wheel required by the Telegram client"
        )
    if release.is_symlink() or release.parent.is_symlink():
        raise ValueError("Unexpected symlink in host release directory")
    if package.exists():
        allowed_files = set(expected) | {
            Path("agentbridge.whl"),
            Path("host.json"),
            Path("telegram.json"),
        }
        package_dirs = {Path(pkg.name) for pkg in sources}
        for path in release.rglob("*"):
            rel = path.relative_to(release)
            if path.is_symlink() or not (
                (path.is_dir() and rel in package_dirs)
                or (path.is_file() and rel in allowed_files)
            ):
                raise ValueError("Existing host archive contains an unexpected entry")
        if (
            not (release / "agentbridge.whl").is_file()
            or sha((release / "agentbridge.whl").read_bytes()) != artifact["sha256"]
        ):
            raise ValueError("Archived central client differs from the verified wheel")
        archived = {
            p.relative_to(release): p.read_bytes()
            for pkg in sources
            for p in (release / pkg.name).glob("*.py")
        }
        if spec.get("coordinatorPython"):
            archived[Path("coordinator")] = (release / "coordinator").read_bytes()
        if archived != expected:
            raise ValueError("Existing host archive differs from the fixed candidate")
        return release
    release.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=release.parent) as tmp:
        staged = Path(tmp) / "release"
        staged.mkdir(mode=0o700)
        (staged / "agentbridge.whl").write_bytes(wheel.read_bytes())
        for rel, blob in expected.items():
            destination = staged / rel
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            destination.write_bytes(blob)
            destination.chmod(0o700 if rel == Path("coordinator") else 0o600)
        staged.rename(release)
    return release


def jobs(spec, mac):
    root = Path(spec["runtimeDir"])
    prefix = "ai.agentbridge.nanobot." + spec["instanceId"]
    launcher = (
        "import runpy,sys;root=sys.argv.pop(1);sys.path[:0]=[root,root+'/agentbridge.whl'];"
        "runpy.run_module(sys.argv.pop(1),run_name='__main__')"
    )

    def launch(module, config):
        return [
            spec["enginePython"],
            "-I",
            "-B",
            "-c",
            launcher,
            str(root / "current"),
            "nanobot_agentbridge." + module,
            "--config",
            str(root / "current" / config),
        ]

    worker = launch("server", "host.json")
    tunnel = mac["tunnel"]
    ssh = [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "UserKnownHostsFile=" + tunnel["knownHostsFile"],
        "-i",
        spec["tunnelIdentityFile"],
        "-R",
        f"127.0.0.1:{spec['remotePort']}:127.0.0.1:{spec['listenPort']}",
        tunnel_user(spec["instanceId"]) + "@" + tunnel["host"],
    ]
    processes = [("worker", worker), ("tunnel", ssh)]
    if spec.get("telegram"):
        processes.append(("telegram", launch("telegram", "telegram.json")))
    return {
        prefix + "." + name: {
            "Label": prefix + "." + name,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProgramArguments": argv,
            "WorkingDirectory": str(root),
            "Umask": 0o077,
        }
        for name, argv in processes
    }


def job_state(label):
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    if (
        "Could not find service" in result.stderr
        or "Could not find service" in result.stdout
    ):
        return False
    raise RuntimeError("Unable to verify launchd job ownership")


def wait_for_running_jobs(labels, spec):
    previous = {}
    for _ in range(20):
        current = {}
        for label in labels:
            info = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
            process = re.search(r"(?m)^\s*pid = ([1-9]\d*)\s*$", info)
            if process and re.search(r"(?m)^\s*state = running\s*$", info):
                current[label] = process[1]
        if len(current) == len(labels) and current == previous:
            if not spec.get("telegram"):
                return
            lock_file = Path(spec["runtimeDir"]) / "telegram" / "poll.lock"
            if lock_file.is_file() and not lock_file.is_symlink():
                with lock_file.open("rb") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return
        previous = current
        time.sleep(0.5)
    raise RuntimeError(
        "NanoBot jobs did not maintain stable processes and channel ownership"
    )


def set_current(root, target):
    if target is None:
        (root / "current").unlink(missing_ok=True)
        return
    tmp = root / ".current-next"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, root / "current")


def managed_jobs(plists):
    result = {}
    for label, value in plists.items():
        target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        body = plistlib.dumps(value)
        running = job_state(label)
        if target.is_symlink() or (
            target.exists()
            and (target.read_bytes() != body or target.stat().st_uid != os.getuid())
        ):
            raise ValueError(
                "Existing launchd configuration has a different owner or definition"
            )
        if running and not target.exists():
            raise ValueError("Unmanaged job uses the requested launchd label")
        if running:
            observed = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
            if not re.search(
                r"(?m)^\s*path = " + re.escape(str(target)) + r"\s*$", observed
            ):
                raise ValueError(
                    "Loaded job does not originate from the approved plist"
                )
        result[label] = (target, body, running)
    return result


def ensure_local_bindings(root, records):
    outputs = []
    credentials = owned_directory(root / "credentials")
    for record in records:
        binding = dict(record["binding"])
        binding.pop("instance_id")
        ref = binding["credential_ref"]
        if not ALIAS.fullmatch(ref):
            raise ValueError("Unexpected central credential reference")
        path = credentials / (ref + ".token")
        secret = record["personalCredential"]
        if path.exists():
            previous = private_file(path).read_bytes()
            if previous != secret.encode():
                if record.get("retiredCredentialDigest") != sha(previous):
                    raise ValueError(
                        "The trusted local credential differs from central issuance"
                    )
                # Only replace a private file whose exact retired generation was verified
                # by central in this admission. Keep an existing valid path for the worker.
                fd, tmp = tempfile.mkstemp(dir=credentials)
                try:
                    with os.fdopen(fd, "w") as output:
                        output.write(secret)
                        output.flush()
                        os.fsync(output.fileno())
                    if private_file(path).read_bytes() != previous:
                        raise ValueError("Personal file changed during admission")
                    os.replace(tmp, path)
                finally:
                    Path(tmp).unlink(missing_ok=True)
        else:
            with os.fdopen(
                os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as f:
                f.write(secret)
        outputs.append({**binding, "tokenFile": str(path)})
    return outputs


def verify_bot(spec):
    if not spec.get("telegram"):
        return
    # Read the selected Keychain reference inside the process. No token or Bot API URL in argv/logs.
    program = (
        "import json,sys;sys.path[:0]=sys.argv[1:3];"
        "from nanobot_agentbridge.telegram import TelegramAPI;"
        "from nanobot_agentbridge.telegram_credentials import bot_token;"
        "s=json.load(sys.stdin);api=TelegramAPI(bot_token({'botCredential':s['botCredential']}));"
        "me=api.call('getMe');w=api.call('getWebhookInfo');"
        "assert me.get('is_bot') is True and me.get('id')==s['botId'] "
        "and me.get('username')==s['username'] and w.get('url')==''"
    )
    run(
        [
            spec["enginePython"],
            "-I",
            "-c",
            program,
            ROOT / "integrations/nanobot-agentbridge",
            ROOT,
        ],
        input=json.dumps(spec["telegram"]),
        timeout=25,
    )


@contextmanager
def pilot_lock(spec):
    runtime = owned_directory(spec["runtimeDir"])
    lock_path = runtime / "native-release.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def admission(pub, spec, mac, artifact):
    with pilot_lock(spec):
        return _admit(pub, spec, mac, artifact)


def configure_release(pub, spec, mac, root, archive, recovery=None):
    extra = {"recovery": recovery} if recovery else {}
    records = remote(pub, spec, "bind", **extra)["bindings"]
    bindings = ensure_local_bindings(root, records)
    for b in bindings:
        b["modelConfigFile"] = spec["modelConfigFile"]
    config = {
        "instanceId": spec["instanceId"],
        "listenPort": spec["listenPort"],
        "serviceTokenFile": spec["serviceTokenFile"],
        "mcpUrl": spec["centralMcpUrl"],
        "caFile": mac["caCertificate"],
        "stateDir": str(root / "state"),
        "modelOAuthTokenFile": spec["modelOAuthTokenFile"],
        "bindings": bindings,
        "trustedCardOrigins": spec["trustedCardOrigins"],
    }
    if spec.get("coordinatorPython"):
        config.update(
            enableSkills=True,
            enableSkillTrials=True,
            coordinatorProgram=str(archive / "coordinator"),
        )
    if spec.get("telegram"):
        bot = spec["telegram"]
        config["telegramPilot"] = {k: bot[k] for k in ("botId", "username")}
        channel = {
            "botId": bot["botId"],
            "botUsername": bot["username"],
            "botCredential": bot["botCredential"],
            "instanceId": spec["instanceId"],
            "hostUrl": f"http://127.0.0.1:{spec['listenPort']}",
            "hostTokenFile": spec["serviceTokenFile"],
            "dataDir": str(root / "telegram"),
            "bindings": {},
            "allowWorkspacePairing": True,
        }
        if (archive / "telegram.json").exists() and read(
            private_file(archive / "telegram.json")
        ) != channel:
            raise ValueError("Existing Telegram release account or ownership differs")
        save(archive / "telegram.json", channel)
    if (archive / "host.json").exists() and read(
        private_file(archive / "host.json")
    ) != config:
        raise ValueError("Existing host release has different bindings/configuration")
    save(archive / "host.json", config)


def retry_proof(pub, spec, root, old, last, definitions, prior):
    if (
        last.get("stage") != "rolled_back"
        or last.get("previousScopePresent") is not False
    ):
        return None
    previous = last["candidate"]
    if re.fullmatch(r"[0-9a-f]{12}", previous) and not prior.get("scopePresent"):
        # A failure before the first bind leaves no pilot to reopen.
        clean = not (root / "releases" / previous / "host.json").exists() and all(
            not os.path.lexists(root / "credentials" / (ref + ".token"))
            for ref in spec["subjects"].values()
        )
        if clean and not last.get("recovery"):
            return None
    if (
        old is not None
        or last.get("previous") is not None
        or not prior.get("scopePresent")
        or not re.fullmatch(r"[0-9a-f]{12}", previous)
        or (previous != pub.release and previous not in pub.policy["compatibleFrom"])
        or set(definitions) != set(last["jobs"])
        or any(
            loaded != last["jobs"][label]["loaded"]
            or path.exists() != last["jobs"][label]["existed"]
            for label, (path, _, loaded) in definitions.items()
        )
        or prior.get("tunnelAuthorized", False) != last["previousTunnelAuthorized"]
    ):
        raise ValueError("Earlier automatic pilot rollback does not match this release")
    previous_proof = last.get("recovery", {})
    anchor = previous_proof.get("archiveCandidate", previous)
    if not re.fullmatch(r"[0-9a-f]{12}", anchor):
        raise ValueError("Unknown original NanoBot release")
    archive_path = root / "releases" / anchor / "host.json"
    if archive_path.resolve() != archive_path:
        raise ValueError("Original pilot archive path differs")
    archive = read(private_file(archive_path))
    if archive["instanceId"] != spec["instanceId"] or len(archive["bindings"]) != len(
        spec["subjects"]
    ):
        raise ValueError("Original pilot host differs from the reviewed instance")
    subjects = {}
    for binding in archive["bindings"]:
        subject = binding["user_subject"]
        ref = spec["subjects"].get(subject)
        if not ref or ref != binding["credential_ref"] or subject in subjects:
            raise ValueError("Original pilot subject assignment differs")
        path = root / "credentials" / (ref + ".token")
        if binding["tokenFile"] != str(path) or path.resolve() != path:
            raise ValueError("Original pilot personal credential path differs")
        fields = (
            "user_subject",
            "endpoint_id",
            "endpoint_key",
            "session_key",
            "credential_ref",
        )
        subjects[subject] = {
            **{k: binding[k] for k in fields},
            "fileDigest": sha(private_file(path).read_bytes()),
        }
    source = last.get("unmodifiedSource")
    proof = {
        "sourceCandidate": source or previous,
        "previousAttempt": (
            previous_proof.get("previousAttempt")
            if source
            else previous_proof.get("attempt")
        ),
        "candidate": pub.release,
        "attempt": uuid.uuid4().hex,
        "archiveCandidate": anchor,
        "subjects": subjects,
    }
    proof["baseRevision"] = remote(pub, spec, "recovery_check", recovery=proof)[
        "baseRevision"
    ]
    # Preserve the private original receipt before the new transaction takes its place.
    history = owned_directory(root / "rollback-history")
    digest = sha(json.dumps(last, sort_keys=True).encode())
    path = history / (previous + "-" + digest + ".json")
    if path.exists():
        if read(private_file(path)) != last:
            raise ValueError("Archived pilot rollback receipt differs")
    else:
        save(path, last)
    return proof


def _admit(pub, spec, mac, artifact):
    root = owned_directory(spec["runtimeDir"])
    definitions = managed_jobs(jobs(spec, mac))
    current = root / "current"
    old = current.resolve(strict=True) if current.is_symlink() else None
    if (old and old.parent != root / "releases") or (
        current.exists() and not current.is_symlink()
    ):
        raise ValueError("Unknown NanoBot release pointer")
    release_state = root / "native-release.json"
    last = read(private_file(release_state)) if release_state.exists() else {}
    if pub.args.resume:
        if last.get("stage") != "confirmed" or last.get("candidate") != pub.release:
            raise ValueError("Resume requires a confirmed NanoBot host transaction")
        if old != root / "releases" / pub.release or not all(
            v[2] for v in definitions.values()
        ):
            raise ValueError(
                "Resume requires the already active verified NanoBot release"
            )
        archive_bridge(pub, spec, artifact)
        wait_for_running_jobs(definitions, spec)
        if not remote(pub, spec, "observe")["tunnelAuthorized"]:
            raise ValueError(
                "The dedicated forwarding identity is no longer authorized"
            )
        return remote(pub, spec, "probe")
    if last.get("stage") in {"switching", "restoring"}:
        raise ValueError(
            "An interrupted NanoBot switch requires explicit host rollback"
        )
    archive = archive_bridge(pub, spec, artifact)
    prior = remote(pub, spec, "observe")
    recovery = retry_proof(pub, spec, root, old, last, definitions, prior)
    domain = f"gui/{os.getuid()}"
    record = {
        "candidate": pub.release,
        "previous": old.name if old else None,
        "previousScopePresent": False if recovery else prior["scopePresent"],
        "previousTunnelAuthorized": prior.get("tunnelAuthorized", False),
        "stage": "switching",
        "jobs": {
            label: {"loaded": loaded, "existed": path.exists()}
            for label, (path, _, loaded) in definitions.items()
        },
    }
    if recovery:
        record["recovery"] = recovery
    if old == archive and all(v[2] for v in definitions.values()):
        if not prior["tunnelAuthorized"]:
            raise ValueError(
                "The dedicated forwarding identity is no longer authorized"
            )
        wait_for_running_jobs(definitions, spec)
        return remote(pub, spec, "probe")
    save(release_state, record)
    try:
        configure_release(pub, spec, mac, root, archive, recovery)
        identities = verify_subjects(spec, archive)
        remote(pub, spec, "tunnel_enable")
        for label, (_, _, running) in reversed(list(definitions.items())):
            if running:
                ctl("bootout", domain + "/" + label)
        import socket

        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", spec["listenPort"])) == 0:
                raise ValueError("The local pilot port is still occupied")
        set_current(root, archive)
        for label, (target, body, _) in definitions.items():
            if label.endswith(".telegram"):
                # The channel can consume messages only after central verifies the worker through its tunnel.
                wait_for_route(pub, spec)
            if not target.exists():
                with os.fdopen(
                    os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
                ) as stream:
                    stream.write(body)
            ctl("enable", domain + "/" + label)
            ctl("bootstrap", domain, str(target))
            if not job_state(label):
                raise RuntimeError("NanoBot supervisor did not retain the new job")
        outcome = wait_for_route(pub, spec)
        wait_for_running_jobs(definitions, spec)
        record["stage"] = "confirmed"
        save(release_state, record)
        return {**outcome, **identities}
    except Exception:
        restore(pub, spec, mac, record)
        raise


def verify_subjects(spec, archive):
    result = json.loads(
        run(
            [
                spec["enginePython"],
                "-I",
                "-B",
                ROOT / "scripts/native/nanobot_identity_probe.py",
                archive,
                spec.get("requiredLevel", "L2"),
            ],
            timeout=90,
        )
    )
    if result != {
        "authenticatedSubjects": 2,
        "acceptedLevel": spec.get("requiredLevel", "L2"),
    }:
        raise ValueError("NanoBot subject verification receipt is incomplete")
    return result


def ctl(*args):
    return run(["/bin/launchctl", *args], timeout=15)


def wait_for_route(pub, spec):
    for _ in range(15):
        try:
            return remote(pub, spec, "probe")
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError(
        "NanoBot did not pass authenticated readiness through the approved tunnel"
    )


def restore(pub, spec, mac, record=None):
    root = owned_directory(spec["runtimeDir"])
    state_file = root / "native-release.json"
    if record is None:
        record = read(private_file(state_file))
    if record.get("candidate") != pub.release or record.get("stage") not in {
        "confirmed",
        "switching",
        "restoring",
    }:
        raise ValueError("NanoBot rollback must target this recorded candidate")
    previous = record["previous"]
    if previous is not None and not re.fullmatch(r"[0-9a-f]{12}", previous):
        raise ValueError("Invalid prior NanoBot release")
    old = root / "releases" / previous if previous else None
    current = root / "current"
    actual = current.resolve() if current.is_symlink() else None
    if actual not in {old, root / "releases" / pub.release}:
        raise ValueError("NanoBot current pointer changed outside this transaction")
    if old and not (old / "host.json").is_file():
        raise ValueError("Missing earlier host release")
    defs = managed_jobs(jobs(spec, mac))
    if set(defs) != set(record["jobs"]):
        raise ValueError("The configured NanoBot jobs changed outside this transaction")
    record["stage"] = "restoring"
    save(state_file, record)
    domain = f"gui/{os.getuid()}"
    for label, (path, _, loaded) in reversed(list(defs.items())):
        if loaded:
            ctl("bootout", domain + "/" + label)
        if not record["jobs"][label]["existed"]:
            ctl("disable", domain + "/" + label)
            path.unlink(missing_ok=True)
    set_current(root, old)
    for label, (path, _, _) in defs.items():
        if record["jobs"][label]["loaded"]:
            ctl("bootstrap", domain, str(path))
    if not record["previousTunnelAuthorized"]:
        remote(pub, spec, "tunnel_disable")
    if record.get("recovery"):
        proof = record["recovery"]
        result = remote(pub, spec, "rollback_scope", recovery=proof)
        if not result["reverted"]:
            record["unmodifiedSource"] = proof["sourceCandidate"]
    elif not record["previousScopePresent"]:
        remote(pub, spec, "rollback_scope")
    elif old:
        wait_for_route(pub, spec)
    record["stage"] = "rolled_back"
    save(state_file, record)
    return {"status": "nanobot_host_rolled_back", "previous": previous}
