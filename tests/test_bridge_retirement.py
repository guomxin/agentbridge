import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbridge.cli.main import main
from agentbridge.core.config import ConfigStore


class BridgeRetirementTests(unittest.TestCase):
    def test_legacy_bridge_entry_points_are_not_public_commands(self):
        for command in (
            ["daemon", "status"],
            ["oa", "status"],
            ["explore", "dom-snapshot", "oa"],
            ["command", "list"],
            ["discovered", "list", "oa"],
            ["mcp", "serve"],
        ):
            with self.subTest(command=command), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(command)
            self.assertEqual(raised.exception.code, 2)

    def test_legacy_bridge_runtime_files_are_absent(self):
        root = Path(__file__).resolve().parents[1]
        for relative_path in (
            "extension/manifest.json",
            "extension/background.js",
            "extension/content.js",
            "agentbridge/browser/bridge.py",
            "agentbridge/daemon/app.py",
            "agentbridge/mcp/server.py",
        ):
            with self.subTest(path=relative_path):
                self.assertFalse((root / relative_path).exists())

    def test_seeyon_profile_uses_central_session_auth(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(["--home", tmp, "system", "init-seeyon-oa"])

        profile = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(profile["auth_mode"], "central_session")

    def test_legacy_profile_metadata_is_rejected(self):
        with TemporaryDirectory() as tmp:
            systems = Path(tmp) / "systems"
            systems.mkdir()
            (systems / "oa.json").write_text(
                json.dumps(
                    {
                        "id": "oa",
                        "name": "Seeyon OA",
                        "base_url": "http://oa.example/seeyon/main.do?method=main",
                        "allowed_origins": ["http://oa.example"],
                        "auth_mode": "chrome_extension",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "auth_mode must be central_session",
            ):
                ConfigStore(Path(tmp)).load_system("oa")

    def test_legacy_bridge_result_flag_is_absent(self):
        root = Path(__file__).resolve().parents[1] / "agentbridge"
        for path in root.rglob("*.py"):
            with self.subTest(path=path.relative_to(root)):
                self.assertNotIn(
                    "browser_bridge_used",
                    path.read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
