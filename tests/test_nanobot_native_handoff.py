"""Native macOS switch and recovery handoff, isolated from installed launch agents."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import select
import socket
import subprocess

import pytest

from tests.test_nanobot_native_publish import host


def synthetic_clock(monkeypatch):
    ticks = [0.0]
    monkeypatch.setattr(host.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(host.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    return ticks


@pytest.mark.parametrize("removed,exited", [(0.3, 1.2), (1.2, 0.3)])
def test_bootout_waits_for_both_original_process_and_registration(monkeypatch, removed, exited):
    ticks = synthetic_clock(monkeypatch)
    calls = []
    path = Path("/synthetic/pilot-worker.plist")

    def snapshot(_):
        return f"path = {path}\npid = 100" if ticks[0] < removed else None

    @contextmanager
    def watch(pid):
        assert pid == 100
        yield lambda: ticks[0] >= exited

    monkeypatch.setattr(host, "launchd_info", snapshot)
    monkeypatch.setattr(host, "process_exit_watch", watch)
    monkeypatch.setattr(host, "ctl", lambda action, *_: calls.append(action))
    host.stop_job("pilot.worker", path, deadline=3)
    assert max(removed, exited) <= ticks[0] < 2 and calls == ["bootout"]


def test_respawn_rearms_before_bootout_and_changed_origin_fails_closed(monkeypatch):
    ticks = synthetic_clock(monkeypatch)
    path = Path("/synthetic/owned")
    values = iter([100, 200, 200, 200])
    watched = []
    stopped = []

    @contextmanager
    def watch(pid):
        watched.append(pid)
        yield lambda: True

    def snapshot(_):
        return None if stopped else f"path = {path}\npid = {next(values)}"

    monkeypatch.setattr(host, "launchd_info", snapshot)
    monkeypatch.setattr(host, "process_exit_watch", watch)
    monkeypatch.setattr(host, "ctl", lambda *args: stopped.append((args, list(watched))))
    host.stop_job("pilot.worker", path, 3)
    assert len(stopped) == 1 and stopped[0][1] == [100, 200] and ticks[0] < 3
    monkeypatch.setattr(host, "launchd_info", lambda _: "path = /synthetic/foreign")
    with pytest.raises(ValueError, match="different origin"):
        host.stop_job("pilot.worker", path, 3)
    assert len(stopped) == 1


def test_bootout_error_is_success_only_after_verified_exit_and_removal(monkeypatch):
    ticks = synthetic_clock(monkeypatch)
    released = [False]
    path = Path("/synthetic/job")
    monkeypatch.setattr(host, "launchd_info", lambda _: None if released[0] and ticks[0] >= .5 else f"path = {path}")

    def already_removing(*_):
        raise RuntimeError("launchctl failed (exit 5)")

    monkeypatch.setattr(host, "ctl", already_removing)
    with pytest.raises(RuntimeError, match="launchd registration"):
        host.stop_job("pilot.worker", path, deadline=.3)
    released[0] = True
    host.stop_job("pilot.worker", path, deadline=1)


@pytest.mark.parametrize("occupied", ["local", "telegram", "remote", "delayed"])
def test_listener_reverse_port_and_poll_lock_gate_pointer_handoff(monkeypatch, tmp_path, occupied):
    ticks = synthetic_clock(monkeypatch)
    spec = {"runtimeDir": str(tmp_path), "listenPort": 12345, "telegram": True}
    stopped = []
    defs = {role: (tmp_path / role, b"", True) for role in ("worker", "tunnel", "telegram")}
    monkeypatch.setattr(host, "stop_job", lambda label, *_: stopped.append(label))
    monkeypatch.setattr(host, "local_port_free", lambda _: occupied != "local")
    monkeypatch.setattr(host, "poll_lock_held", lambda _: occupied == "telegram")
    monkeypatch.setattr(host, "remote", lambda *args: {"portOccupied": occupied == "remote" or ticks[0] < .5})
    if occupied == "delayed":
        host.drain_jobs(None, spec, defs)
        assert .5 <= ticks[0] < 1
    else:
        with pytest.raises(RuntimeError, match="remained occupied"):
            host.drain_jobs(None, spec, defs)
    assert stopped == ["telegram", "tunnel", "worker"]


def test_running_jobs_require_exact_resource_owner_and_loopback(monkeypatch, tmp_path):
    spec = {"runtimeDir": str(tmp_path), "listenPort": 22334}
    lock = tmp_path / "telegram/poll.lock"
    lock.parent.mkdir()
    lock.touch()
    observed = {"worker": "p10\nf8\nn127.0.0.1:22334", "telegram": f"p11\nf7\nn{lock.resolve()}"}
    monkeypatch.setattr(host, "run", lambda argv, **kw: observed["telegram" if "--" in argv else "worker"])
    pids = {"test.worker": 10, "test.telegram": 11}
    assert not host.owns_runtime_resources(pids, spec)
    with lock.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        assert host.owns_runtime_resources(pids, spec)
        observed["telegram"] += f"\np15\nf9\nn{lock.resolve()}"
        assert not host.owns_runtime_resources(pids, spec)
        observed["worker"] = "p10\nf8\nn*:22334"
        assert not host.owns_runtime_resources({"test.worker": 10}, spec)


@pytest.mark.skipif(not hasattr(select, "kqueue") or not Path("/usr/sbin/lsof").is_file(), reason="Darwin kqueue / lsof")
def test_actual_process_generation_and_open_descriptors_on_mac(tmp_path):
    process = subprocess.Popen(["/bin/sleep", "0.45"])
    try:
        with host.process_exit_watch(process.pid) as exited:
            assert not exited()
            process.wait(timeout=3)
            assert exited()
    finally:
        process.wait(timeout=3)
    lock = tmp_path / "telegram/poll.lock"
    lock.parent.mkdir()
    with socket.socket() as listener, lock.open("wb") as poller:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        fcntl.flock(poller, fcntl.LOCK_EX)
        spec = {"listenPort": port, "runtimeDir": str(tmp_path)}
        assert not host.local_port_free(port)
        assert host.owns_runtime_resources({"test.worker": os.getpid(), "test.telegram": os.getpid()}, spec)
        assert not host.owns_runtime_resources({"test.telegram": os.getppid()}, spec)
    assert host.local_port_free(port) and not host.poll_lock_held(lock)


@pytest.mark.skipif(not hasattr(select, "kqueue") or not Path("/bin/launchctl").is_file(), reason="macOS launchd")
def test_two_real_launchd_generations_share_neither_listener_nor_consumer(tmp_path, monkeypatch):
    import plistlib
    import sys
    import time
    import uuid

    domain = f"gui/{os.getuid()}"
    if subprocess.run(["/bin/launchctl", "print", domain], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        pytest.skip("This session has no launchd GUI domain")
    root = tmp_path.resolve()
    lock = root / "telegram/poll.lock"
    lock.parent.mkdir()
    script = root / "isolated_job.py"
    script.write_text(
        "import fcntl, pathlib, signal, socket, sys, time\n"
        "role, number, lock_path = sys.argv[1:]\n"
        "def finish(*_):\n"
        "    time.sleep(.6)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, finish)\n"
        "if role == 'telegram':\n"
        "    handle = pathlib.Path(lock_path).open('ab')\n"
        "    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "else:\n"
        "    listener = socket.socket()\n"
        "    listener.bind(('127.0.0.1', int(number)))\n"
        "    listener.listen()\n"
        "while True: time.sleep(.1)\n"
    )
    with socket.socket() as local, socket.socket() as reverse:
        local.bind(("127.0.0.1", 0))
        reverse.bind(("127.0.0.1", 0))
        port, reverse_port = local.getsockname()[1], reverse.getsockname()[1]
    spec = {"listenPort": port, "runtimeDir": str(root), "telegram": True}
    prefix = "ai.agentbridge.acceptance." + uuid.uuid4().hex
    definitions = {}
    for role in ("worker", "tunnel", "telegram"):
        label = prefix + "." + role
        path = root / (label + ".plist")
        body = plistlib.dumps({"Label": label, "RunAtLoad": True, "ProgramArguments": [
            sys.executable, "-I", "-B", str(script), role, str(reverse_port if role == "tunnel" else port), str(lock)]})
        path.write_bytes(body)
        definitions[label] = path, body, True
    monkeypatch.setattr(host, "remote", lambda *_: {"portOccupied": not host.local_port_free(reverse_port)})
    generations = []
    try:
        for _ in range(2):
            for label, (path, _, _) in definitions.items():
                host.ctl("bootstrap", domain, str(path))
            host.wait_for_running_jobs(definitions, spec)
            generations.append({host.job_pid(host.launchd_info(label)) for label in definitions})
            started = time.monotonic()
            host.drain_jobs(None, spec, definitions)
            assert time.monotonic() - started >= .6
            assert all(not host.job_state(label) for label in definitions)
            assert host.local_port_free(port) and host.local_port_free(reverse_port) and not host.poll_lock_held(lock)
        assert all(len(pids) == 3 for pids in generations) and generations[0].isdisjoint(generations[1])
    finally:
        for label in reversed(definitions):
            if host.job_state(label):
                host.stop_job(label, definitions[label][0], time.monotonic() + 20)
