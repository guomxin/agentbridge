import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentProfileTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Legacy Windows PowerShell entrypoint; outside macOS/Linux maintenance")
    def test_explicit_profile_binds_target_and_unit_bytes(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell required for deployment entry test')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = {}
            for name in ('agentbridge.service', 'agentbridge-backup.service', 'agentbridge-backup.timer'):
                path = root / name
                path.write_text('[Unit]\nDescription=fixture\n')
                units[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            profile = root / 'environment.json'
            profile.write_text(json.dumps(dict(schema='agentbridge.environment.v1', host='example.test',
                                              root='/home/example/service', units=units)))
            script = root / 'probe.ps1'
            script.write_text('''param($Module, $Profile, $Repo)
$ErrorActionPreference = 'Stop'
Import-Module $Module -Force
Read-AgentBridgeEnvironment -Path $Profile -RepoRoot $Repo -HostName 'example.test' -RemoteRoot '/home/example/service' | Out-Null
''')
            args = [shell, '-NoProfile', '-File', str(script),
                    str(ROOT / 'scripts/AgentBridgeEnvironment.psm1'), str(profile), str(ROOT)]
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
            (root / 'agentbridge.service').write_text('changed')
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)


def test_secret_scanner_detects_patterns_without_returning_secret_values():
    from scripts.check_public_content import findings
    assert findings('safe\n' + 'AKIA' + 'A' * 16) == [2]
    assert findings('a normal configuration value') == []
