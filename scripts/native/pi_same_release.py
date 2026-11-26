"""Restore only a previously published Pi on its still-active central release."""

from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import stat
import zipfile
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ORIGINS = {
    prefix + repo + ".git"
    for prefix in ("https://github.com/", "git@github.com:")
    for repo in ("guomxin/agentbridge", "guomxin/cli-helper")
}


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        env=env,
        timeout=90,
    )
    if result.returncode:
        raise ValueError("Unable to verify release Git history or the approved GitHub remote")
    return result.stdout


def exact_source(trusted, source):
    if Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel")).strip()).resolve() != source:
        raise ValueError("Expected the root of the original checkout")
    commit = git(source, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid source commit")
    git(trusted, "merge-base", "--is-ancestor", commit, "HEAD")
    if git(source, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise ValueError("Prior checkout contains local edits or untracked files")
    if (source / ".gitrepo").exists():
        raise ValueError("Unexpected alternative metadata in prior checkout")
    tracked = set()
    for entry in git(trusted, "ls-tree", "-rz", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw = entry.split(b"\t", 1)
        mode, kind, digest = metadata.split()
        relative = Path(os.fsdecode(raw))
        tracked.add(relative)
        path = source / relative
        if (
            kind != b"blob"
            or mode not in (b"100644", b"100755")
            or path.resolve() != path
            or not path.is_file()
            or bool(path.stat().st_mode & 0o111) != (mode == b"100755")
        ):
            raise ValueError("Prior checkout does not contain the expected regular file")
        contents = path.read_bytes()
        if hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest().encode() != digest:
            raise ValueError("Prior checkout differs from the trusted Git tree")
    # Full creates bytecode, build output, and installed Node dependencies. None
    # is an import root. Use an empty isolated cache in the bridge subprocess.
    for parent, dirs, files in os.walk(source):
        here = Path(parent).relative_to(source)
        dirs[:] = [
            d
            for d in dirs
            if d not in {"__pycache__", "node_modules"}
            and not (here == Path(".") and d in {".git", ".venv", "output", "build"})
        ]
        for name in [*dirs, *files]:
            relative = here / name
            if relative not in tracked and (
                (source / relative).is_symlink()
                or relative.suffix in {".py", ".pyw", ".pyc", ".pyo", ".so", ".dylib", ".pth", ".pyd"}
            ):
                raise ValueError("Unexpected executable or import in original checkout")
    return commit


def published_ancestor(trusted, commit, remote_head):
    if not re.fullmatch(r"[0-9a-f]{40}", remote_head):
        raise ValueError("Invalid GitHub main")
    git(trusted, "merge-base", "--is-ancestor", commit, remote_head)
    changes = git(
        trusted, "log", "-m", "--format=", "--no-renames", "--name-only", "-z", commit + ".." + remote_head
    )
    for raw in changes.split(b"\0"):
        path = os.fsdecode(raw)
        if path and not (path in {"README.md", "AGENTS.md"} or path.startswith("docs/") and path.endswith(".md")):
            raise ValueError("Later published main includes a non-documentation change")


def published_main(args, commit):
    from common import external, private_file

    origins = [git(ROOT, "remote", "get-url", *flags, "origin").decode().strip() for flags in ([], ["--push"])]
    if any(origin not in ORIGINS for origin in origins):
        raise ValueError("Unexpected GitHub origin")
    origin = origins[0]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    if origin.startswith("git@"):
        known = external(args.github_known_hosts) if args.github_known_hosts else None
        if (
            not args.github_identity
            or not known
            or not re.search(r"(?m)^github\.com ssh-ed25519 ", known.read_text())
        ):
            raise ValueError("Explicit GitHub identity and pinned known hosts required")
        env["GIT_SSH_COMMAND"] = shlex.join(
            [
                "ssh",
                "-F",
                os.devnull,
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(known),
                "-i",
                str(private_file(args.github_identity)),
            ]
        )
    with tempfile.TemporaryDirectory() as tmp:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(tmp).parent)
        ref = (
            git(
                tmp,
                "-c",
                "http.sslVerify=true",
                "-c",
                "http.followRedirects=false",
                "ls-remote",
                "--exit-code",
                origin,
                "refs/heads/main",
                env=env,
            )
            .decode()
            .split()
        )
    if len(ref) != 2 or ref[1] != "refs/heads/main":
        raise ValueError("Could not identify live GitHub main")
    published_ancestor(ROOT, commit, ref[0])
    return ref[0]


def validate_mode(args):
    if (
        args.offline
        or args.resume
        or args.rollback_pi
        or args.rollback_nanobot
        or not args.host_profile
        or not args.authorization
        or not args.pi_profile
    ):
        raise ValueError("Pi recovery needs reviewed Pi and host authorization and an online standalone operation")


def restore_published(args):
    validate_mode(args)
    source = Path(args.restore_pi_from).expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory() as cache:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "GIT_"))}
        env.update(
            PYTHONPYCACHEPREFIX=cache,
            PYTHONDONTWRITEBYTECODE="1",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.fsmonitor",
            GIT_CONFIG_VALUE_0="false",
        )
        child = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "pycache_prefix=" + cache, __file__],
            cwd=source,
            env=env,
            input=json.dumps(vars(args)),
            capture_output=True,
            text=True,
            timeout=1800,
        )
    if child.returncode:
        raise RuntimeError("Same-release Pi recovery failed in the verified prior checkout")
    report = json.loads(child.stdout)
    if report.get("status") == "failed":
        raise ValueError(report["reason"])
    return report


CENTRAL_VERIFY = """
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]);release=sys.argv[2];active=root/'current'
if not active.is_symlink() or active.resolve(strict=True)!=root/'releases'/release:
    raise SystemExit(2)
env=(root/'config/release.env').read_text().splitlines()
if [x for x in env if x.startswith('AGENTBRIDGE_RELEASE_ID=')]!=['AGENTBRIDGE_RELEASE_ID='+release]:
    raise SystemExit(2)
art=json.loads((active/'artifact.json').read_text())
deployment=json.loads((active/'deployment.json').read_text())
if deployment.get('status')!='confirmed' or deployment.get('releaseId')!=release:
    raise SystemExit(2)
wheels=list(active.glob('*.whl'))
if len(wheels)!=1 or wheels[0].is_symlink() or hashlib.sha256(wheels[0].read_bytes()).hexdigest()!=art['sha256']:
    raise SystemExit(2)
print(json.dumps([art['commit'],art['sha256']]))
"""


def central_active(pub, artifact):
    command = shlex.join(["python3", "-I", "-c", CENTRAL_VERIFY, pub.args.remote_root, pub.release])
    if json.loads(pub.ssh("systemctl is-active --quiet agentbridge.service && " + command)) != [
        pub.commit,
        artifact["sha256"],
    ]:
        raise ValueError("Active central commit or deployed wheel differs from the reviewed prior release")


def wheel_members(path):
    members = {}
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = item.filename
            mode = (item.external_attr >> 16) & 0xFFFF
            if (
                name in members
                or Path(name).is_absolute()
                or ".." in Path(name).parts
                or stat.S_IFMT(mode) not in {0, stat.S_IFDIR, stat.S_IFREG}
            ):
                raise ValueError("Invalid or duplicate release wheel member")
            members[name] = (mode, item.is_dir(), hashlib.sha256(archive.read(item)).digest())
    if len([name for name in members if name.endswith(".dist-info/RECORD")]) != 1:
        raise ValueError("Missing release wheel RECORD")
    return members


def original_artifact(pub, spec, rebuilt):
    from common import private_file

    path = Path(spec["runtimeDir"]) / "releases" / pub.release / "agentbridge.whl"
    if path.resolve() != path:
        raise ValueError("Original Pi archive must not traverse a symlink")
    original = private_file(path)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    if digest != rebuilt["sha256"] and wheel_members(original) != wheel_members(Path(rebuilt["wheel"])):
        raise ValueError("Rebuilt wheel differs from the published release contents")
    return {"wheel": str(original), "sha256": digest}


def archive_probe(pub, spec, artifact):
    from pi_release import ADAPTER, SDK_VERSION, engine_identity
    from scripts.run_pi_conformance import run_archive_probe

    pins = {
        "commit": pub.commit,
        "wheelSha256": artifact["sha256"],
        "adapterVersion": ADAPTER,
        "sdkVersion": SDK_VERSION,
        "engine": engine_identity(spec["enginePython"]),
        **{k: spec[k] for k in ("nodeSha256", "sdkTreeSha256", "sdkLockSha256")},
    }
    runtime = Namespace(node=spec["nodePath"], sdk_dir=spec["sdkDir"], engine_python=spec["enginePython"])
    proof = run_archive_probe(runtime, artifact, pins)
    if not proof:
        raise ValueError("Published wheel failed its isolated native Pi worker archive probe")
    return proof["measurements"]


def source_state(pub, spec, observed):
    import pi_local
    from common import read, private_file

    root = Path(spec["runtimeDir"])
    record = read(private_file(root / "native-release.json"))
    current = pi_local.current_target(root)
    stage = record.get("stage")
    if (
        record.get("candidate") != pub.release
        or record.get("previous") is not None
        or record.get("previousScopePresent") is not False
        or not observed.get("scopePresent")
        or stage not in {"rolled_back", "confirmed"}
        or not (root / "releases" / pub.release / "host.json").is_file()
    ):
        raise ValueError("Expected the recorded same-release Pi rollback or its confirmed restoration")
    if stage == "rolled_back":
        if (
            current is not None
            or observed.get("portOccupied")
            or observed.get("tunnelAuthorized") != record["previousTunnelAuthorized"]
        ):
            raise ValueError("Old Pi still owns a live runtime, port, or tunnel")
    elif current != root / "releases" / pub.release:
        raise ValueError("Confirmed Pi points to a different release")
    return record


def validate_closed(pub, spec, mac, record, observed):
    import nanobot_release, pi_local, pi_release

    root = Path(spec["runtimeDir"])
    _, definitions = pi_local.job_sets(spec, mac)
    pi_local.check_predecessor(pub, spec, None, record)

    class Checked(Exception):
        pass

    def verify(publisher, profile, action, **payload):
        if action != "recovery_check":
            raise ValueError("Unexpected mutation during read-only recovery validation")
        revision = pi_release.remote(publisher, profile, action, **payload)["baseRevision"]
        if type(revision) is not int or revision < 2 or revision % 2:
            raise ValueError("Expected an audited revoked Pi revision")
        # The older implementation archives the local receipt after this callback.
        # A plan must stop once the real authority has validated the closed scope.
        raise Checked

    try:
        nanobot_release.retry_proof(pub, spec, root, None, record, definitions, observed, recovery_remote=verify)
    except Checked:
        return
    raise ValueError("Original Pi rollback has no reviewed identity recovery chain")


def old_transaction(args):
    import lifecycle, nanobot_release, pi_local, pi_release
    from publish import Publisher, candidate_artifact

    pub = Publisher(args)
    if pub.commit != args.verifiedCommit or not pub.pi:
        raise ValueError("Reviewed Pi belongs to another release")
    mac = lifecycle.settings(args.host_profile)
    lifecycle.authorize(mac, args.authorization)
    rebuilt, _ = candidate_artifact(require_receipt=True)
    spec = pub.pi
    artifact = original_artifact(pub, spec, rebuilt)
    central_active(pub, artifact)
    if not pi_release.reviewed_contract(pub, spec, artifact)["approved"]:
        raise ValueError("Original Pi qualification failed")
    if args.apply:
        pi_release.verify_conformance(pub, spec, artifact)
    measurements = archive_probe(pub, spec, artifact)
    nanobot_release.verify_bot(spec)
    observed = pi_release.remote(pub, spec, "observe")
    record = source_state(pub, spec, observed)
    if record["stage"] == "rolled_back":
        validate_closed(pub, spec, mac, record, observed)
    result = {
        "status": "planned",
        "commit": pub.commit,
        "githubMain": args.verifiedMain,
        "piStage": record["stage"],
        "pushed": False,
        "centralDeployed": False,
        "originalWheelSha256": artifact["sha256"],
        "rebuiltWheelSha256": rebuilt["sha256"],
        "archiveProbe": measurements,
    }
    if args.apply:
        if pi_release.profile_for(pub) != spec:
            raise ValueError("Pi profile changed during verification")
        lifecycle.authorize(mac, args.authorization)
        central_active(pub, artifact)
        checked = False

        def guarded(publisher, profile, action, **payload):
            nonlocal checked
            if not checked and action != "observe":
                raise ValueError("Pi admission must observe ownership under its release lock")
            if action == "provision" and not payload.get("recovery"):
                raise ValueError("Same-release restoration requires the original recovery chain")
            response = pi_release.remote(publisher, profile, action, **payload)
            if not checked:
                # admission repeats observe under its release lock before provisioning.
                if source_state(pub, spec, response) != record:
                    raise ValueError("Pi rollback record changed during validation")
                checked = True
            return response

        result["piPilot"] = pi_local.admission(pub, spec, mac, artifact, remote=guarded)
        if result["piPilot"].get("authenticatedSubjects") != 2 or result["piPilot"].get("ready") is not True:
            raise ValueError("Both reviewed Pi identities and remote readiness are required")
        result["status"] = "pi_host_restored"
    return result


if __name__ == "__main__":
    # Isolated child uses verified old maintenance modules with their original
    # ROOT, release archive and exact Full. No global module aliasing in the CLI.
    try:
        args = Namespace(**json.load(sys.stdin))
        validate_mode(args)
        source = Path(args.restore_pi_from).expanduser().resolve(strict=True)
        if source != Path.cwd().resolve() or not sys.flags.isolated:
            raise ValueError("Recovery requires the isolated checkout subprocess")
        args.verifiedCommit = exact_source(ROOT, source)
        sys.path[:0] = [str(source / "scripts/native"), str(source)]
        args.verifiedMain = published_main(args, args.verifiedCommit)
        result = old_transaction(args)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        result = {
            "status": "failed",
            "reason": str(error)
            if type(error) in (RuntimeError, ValueError)
            else "Old release verification failed: " + type(error).__name__,
        }
    print(json.dumps(result))
