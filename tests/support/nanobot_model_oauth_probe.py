"""Check the installed NanoBot OAuth storage path and concurrent refresh using simulated tokens only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))


def refresh_worker(counter):
    from oauth_cli_kit import flow
    from oauth_cli_kit.models import OAuthToken
    from nanobot.providers.openai_codex_provider import get_codex_token

    def fake_refresh(refresh, provider, proxy):
        assert refresh == "synthetic-refresh"
        with counter.open("a") as log:
            log.write("1\n")
        time.sleep(0.15)
        return OAuthToken(
            access="synthetic-new",
            refresh="synthetic-next",
            expires=int(time.time() * 1000) + 3600000,
        )

    flow._refresh_token = fake_refresh
    result = get_codex_token()
    assert result.access == "synthetic-new"


def run():
    from nanobot_agentbridge.server import Host

    with tempfile.TemporaryDirectory(prefix="nanobot-synthetic-oauth-") as tmp:
        root = Path(tmp).resolve()
        oauth = root / "model" / "oauth.json"
        oauth.parent.mkdir(mode=0o700)

        def private(path, value):
            path.write_text(value)
            path.chmod(0o600)
            return str(path)

        private(
            oauth,
            json.dumps(
                {
                    "access": "synthetic-old",
                    "refresh": "synthetic-refresh",
                    "expires": 0,
                }
            ),
        )
        cfg = private(
            root / "config.json",
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": "openai-codex/demo",
                            "provider": "openai_codex",
                        }
                    }
                }
            ),
        )
        service = private(
            root / "service", "isolated-service-contract-value-" + ("x" * 16)
        )
        records = []
        for label in ("a", "b"):
            records.append(
                {
                    "user_subject": label,
                    "endpoint_id": "e-" + label,
                    "endpoint_key": "w-" + label,
                    "session_key": "s-" + label,
                    "credential_ref": "personal-" + label,
                    "tokenFile": private(root / label, "synthetic-" + label),
                    "modelConfigFile": cfg,
                }
            )
        host = Host(
            {
                "instanceId": "oauth-probe",
                "mcpUrl": "http://127.0.0.1:1/mcp",
                "serviceTokenFile": service,
                "bindings": records,
                "stateDir": str(root / "state"),
                "modelOAuthTokenFile": str(oauth),
            }
        )
        children = []
        homes = []
        for r in records:
            directory = Path(host.worker_payload(r, "unused")["userDir"])
            env = host.worker_environment(directory)
            assert env["OAUTH_CLI_KIT_TOKEN_PATH"] == str(oauth) and env["HOME"] == str(
                directory
            )
            homes.append(directory)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        str(root / "refreshes"),
                    ],
                    env=env,
                    cwd=directory,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        assert all(p.wait(timeout=30) == 0 for p in children)
        assert (root / "refreshes").read_text().splitlines() == ["1"]
        assert homes[0] != homes[1] and all(
            not any(p.is_file() for p in d.rglob("*")) for d in homes
        )
        assert oauth.stat().st_mode & 0o077 == 0
        print(
            json.dumps(
                {
                    "sdk": "0.4.2",
                    "shared_model_oauth": True,
                    "refreshes": 1,
                    "workers": 2,
                    "personal_state_separate": True,
                    "no_token_copies": True,
                    "real_model_oauth": False,
                }
            )
        )


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) > 1:
        refresh_worker(Path(sys.argv[1]))
    else:
        run()
