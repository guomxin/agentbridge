"""Run an isolated Pi Workspace against the prepared, first-account-only desktop Bot.

This opt-in fixture holds the deployed Bot's own poll lock. Its model, two
accounts, OA, route, pairing, and test inbox are temporary. Only the Telegram
transport is live. Operator opens the printed fixture pages and the named Bot
in Telegram Desktop. Starting this fixture never starts a production host.
"""
import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading

from tests.support.interaction_acceptance import free_port
from tests.support.pi_sdk_probe import ROOT, host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture


def prepared_channel(preparation):
    sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))
    from nanobot_agentbridge.private_files import private_text
    spec = json.loads(private_text(preparation))
    tg = spec["telegram"]
    allowed = tg["allowedPrivateSenders"]
    if (spec.get("preparationOnly") is not True or tg.get("enabled") is not False
            or not isinstance(allowed, dict) or len(allowed) != 1 or len(set(spec["workspaceSubjects"])) != 2
            or not set(allowed).issubset(spec["workspaceSubjects"])):
        raise ValueError("Expected the private pre-deployment two-web, one-desktop preparation")
    first, sender = next(iter(allowed.items()))
    owners = {"a": first, "b": (set(spec["workspaceSubjects"]) - {first}).pop()}
    return tg, sender, owners


@contextmanager
def exclusive_channel(preparation, tg):
    from nanobot_agentbridge.private_files import private_directory
    runtime = private_directory(preparation.parent.parent)
    state = private_directory(runtime / "telegram")
    if state.resolve() != Path(tg["stateDir"]).resolve() or os.path.lexists(runtime / "current"):
        raise ValueError("Desktop fixture requires the original prepared Bot before production activation")
    descriptor = os.open(state / "poll.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "ab") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class DesktopChannel:
    def __init__(self, tg, sender, engine):
        self.bot = int(tg["botId"])
        self.username = tg["botUsername"]
        self.credential = tg["botCredential"]
        self.sender = sender
        self.engine = engine
        self.central_bot = (self.bot, self.username, sender)

    def pilot(self, accounts):
        return {"telegramPilot": {"botId": self.bot, "username": self.username,
                                  "allowedSenders": {accounts["a"]["user_subject"]: self.sender}}}

    @contextmanager
    def consumer(self, directory, port, token, accounts):
        channel = {"hostType": "pi", "adapterVersion": "0.1.7", "enabled": True, "preparationOnly": False,
                   "botId": self.bot, "botUsername": self.username, "botCredential": self.credential,
                   "instanceId": "pi-office", "hostUrl": f"http://127.0.0.1:{port}", "hostTokenFile": token,
                   "dataDir": str(directory / "channel"), "bindings": {}, "allowWorkspacePairing": True,
                   "allowedSenders": self.pilot(accounts)["telegramPilot"]["allowedSenders"]}
        config = private(directory / "channel.json", channel)
        paths = [str(ROOT), str(ROOT / "integrations/nanobot-agentbridge")]
        command = [str(self.engine), "-I", "-B", "-c",
                   "import runpy,sys;sys.path[:0]=sys.argv[1:3];del sys.argv[1:3];"
                   "runpy.run_module('nanobot_agentbridge.telegram',run_name='__main__')", *paths, "--config", config]
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            def check():
                if child.poll() is not None:
                    raise RuntimeError("Isolated Telegram consumer exited")
            try:
                print(json.dumps({"desktopBot": self.username, "fixture": str(directory)}), flush=True)
                yield check
            finally:
                child.terminate()
                try:
                    child.wait(10)
                except subprocess.TimeoutExpired:
                    child.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "sdk-dir", "engine-python", "preparation"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--batch", choices=("read", "credentials", "actions", "media", "skills"), default="read")
    parser.add_argument("--workspace-only", action="store_true", help="Observe the two reviewed Web subjects without starting the Bot")
    args = parser.parse_args()
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if args.workspace_only and args.batch != "read":
        parser.error("--workspace-only qualifies the two read-only Web accounts")
    preparation = args.preparation.resolve()
    tg, sender, owners = prepared_channel(preparation)
    with (nullcontext() if args.workspace_only else exclusive_channel(preparation, tg)), tempfile.TemporaryDirectory(prefix="pi-desktop-") as tmp:
        directory = Path(tmp)
        desktop = DesktopChannel(tg, sender, args.engine_python)
        if args.batch == "credentials":
            from tests.support.pi_credential_probe import exercise
            exercise(directory, args.node, args.sdk_dir, serve=True, desktop=desktop)
            return
        if args.batch in ("actions", "media", "skills"):
            from tests.support.pi_governed_probe import exercise
            scenario = None
            if args.batch == "media":
                from types import SimpleNamespace
                from tests.support.pi_workspace_images_probe import Model
                scenario = SimpleNamespace(model=lambda central: Model())
            exercise(args.node, args.sdk_dir, args.engine_python, args.batch, serve=True, scenario=scenario, desktop=desktop)
            return
        port = free_port()
        host_url = f"http://127.0.0.1:{port}"
        token = private(directory / "route.token", secrets.token_urlsafe(40))
        with central_fixture(directory, host_url=host_url, token_file=token,
                             central_options={"subjects": owners}, desk_bot=desktop.central_bot) as (meta, accounts):
            with host_fixture(directory, args.node, args.sdk_dir, meta, accounts, port=port, token_file=token,
                              options={} if args.workspace_only else desktop.pilot(accounts), engine_python=args.engine_python), (
                    nullcontext() if args.workspace_only else desktop.consumer(directory, port, token, accounts)) as check_channel:
                print(json.dumps({"signin": meta["signin"], "fixture": str(directory)}), flush=True)
                while not stop.wait(.5):
                    if check_channel:
                        check_channel()


if __name__ == "__main__":
    main()
