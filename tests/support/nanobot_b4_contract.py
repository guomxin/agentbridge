"""H13/H14 isolated real-transport acceptance. Stdout contains only the final machine receipt."""

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path

from tests.support import nanobot_b4_files, nanobot_b4_notifications, nanobot_chat_sync_probe

ROOT = Path(__file__).resolve().parents[2]


def stop(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=4)


def wait_for(process, resolve, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError("Isolated service exited before it was ready")
        result = resolve()
        if result:
            return result
        time.sleep(0.12)
    raise TimeoutError("Isolated service did not become ready")


@contextmanager
def environment(python):
    with (
        tempfile.TemporaryDirectory(prefix="nanobot-file-delivery-") as folder,
        ExitStack() as stack,
    ):
        root = Path(folder).resolve()
        env = {
            "HOME": str(root),
            "TMPDIR": str(root),
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
        }

        def start(cmd):
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            stack.callback(stop, proc)
            return proc

        central = start(
            [
                sys.executable,
                "-m",
                "tests.support.workspace_host_acceptance",
                "--parent",
                str(root),
                "--b4",
            ]
        )
        info = wait_for(
            central,
            lambda: next(root.glob("agentbridge-workspace-hosts-*/public.json"), None),
        )
        home = info.parent
        coordinator = root / "coordinator"
        coordinator.write_text(
            "#!"
            + sys.executable
            + " -I\nimport sys\nsys.path.insert(0,"
            + repr(str(ROOT / "integrations/reference-host"))
            + ")\nfrom reference_host.bound_coordinator import main\nmain()\n"
        )
        coordinator.chmod(0o700)
        worker = start(
            [
                str(python),
                str(ROOT / "tests/support/nanobot_b4_worker.py"),
                "--home",
                str(home),
                "--coordinator",
                str(coordinator),
            ]
        )
        wait_for(
            worker,
            lambda: (home / "b4host" / "telegram" / "fixture-ports.json").exists(),
        )
        yield home


def run(python):
    with environment(python) as home:
        chat_sync = nanobot_chat_sync_probe.run(home)
        output = io.StringIO()
        with redirect_stdout(output):
            nanobot_b4_files.run(home, expired_lease=True)
            nanobot_b4_notifications.run(home)
        pdf, csv, tg = [json.loads(line) for line in output.getvalue().splitlines()]
        return {
            "H13": {"passed": True, "pdf": pdf, "csv": csv},
            "H14": {"passed": True, "chatSync":chat_sync, **tg},
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().nanobot_python)))
