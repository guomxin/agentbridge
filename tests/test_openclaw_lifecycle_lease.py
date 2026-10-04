import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "AgentBridgeOpenClawLifecycleLease.psm1"


@unittest.skipUnless(os.name == "nt", "Legacy Windows PowerShell entrypoint; outside macOS/Linux maintenance")
class OpenClawLifecycleLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = (
            shutil.which("powershell.exe")
            or shutil.which("powershell")
            or shutil.which("pwsh")
        )
        if not cls.powershell:
            raise unittest.SkipTest("PowerShell is unavailable")

    def _run(self, script: str, *, lease_path: Path) -> dict:
        environment = {
            "AGENTBRIDGE_TEST_MODULE": str(MODULE),
            "AGENTBRIDGE_TEST_LEASE": str(lease_path),
        }
        stdout_path = lease_path.with_suffix(".stdout")
        stderr_path = lease_path.with_suffix(".stderr")
        with stdout_path.open("w+", encoding="utf-8") as stdout_file, (
            stderr_path.open("w+", encoding="utf-8")
        ) as stderr_file:
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                env={**os.environ, **environment},
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
        if result.returncode != 0:
            self.fail(stderr or stdout)
        return json.loads(stdout.strip().splitlines()[-1])

    def test_fresh_heartbeat_keeps_a_slow_start_lease_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease_path = Path(directory) / "lifecycle.json"
            active = self._run(
                """
Import-Module $env:AGENTBRIDGE_TEST_MODULE -Force
$started = [DateTimeOffset]::UtcNow.AddMinutes(-10)
Set-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 `
    -OperationId 'slow-start' `
    -Action 'restart' `
    -State 'active' `
    -Phase 'waiting_for_readiness' `
    -StartedAt $started | Out-Null
Get-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 | ConvertTo-Json -Compress
""",
                lease_path=lease_path,
            )
            self.assertTrue(active["active"])
            self.assertEqual("active", active["reason"])
            self.assertEqual("waiting_for_readiness", active["phase"])
            self.assertLess(active["heartbeatAgeSeconds"], 5)

            orphaned = self._run(
                """
Import-Module $env:AGENTBRIDGE_TEST_MODULE -Force
Get-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 | ConvertTo-Json -Compress
""",
                lease_path=lease_path,
            )
            self.assertFalse(orphaned["active"])
            self.assertEqual("owner_missing", orphaned["reason"])

    def test_completed_operation_no_longer_blocks_guard_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease_path = Path(directory) / "lifecycle.json"
            completed = self._run(
                """
Import-Module $env:AGENTBRIDGE_TEST_MODULE -Force
$started = [DateTimeOffset]::UtcNow
Set-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 `
    -OperationId 'completed-start' `
    -Action 'restart' `
    -State 'active' `
    -Phase 'waiting_for_readiness' `
    -StartedAt $started | Out-Null
Set-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 `
    -OperationId 'completed-start' `
    -Action 'restart' `
    -State 'completed' `
    -Phase 'completed' `
    -StartedAt $started | Out-Null
Get-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 | ConvertTo-Json -Compress
""",
                lease_path=lease_path,
            )
            self.assertFalse(completed["active"])
            self.assertEqual("inactive", completed["reason"])
            self.assertEqual("completed", completed["state"])
            self.assertEqual([], list(lease_path.parent.glob("*.tmp")))

    def test_expired_lease_does_not_block_guard_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease_path = Path(directory) / "lifecycle.json"
            expired = self._run(
                """
Import-Module $env:AGENTBRIDGE_TEST_MODULE -Force
$started = [DateTimeOffset]::UtcNow.AddMinutes(-10)
Set-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 `
    -OperationId 'expired-start' `
    -Action 'restart' `
    -State 'active' `
    -Phase 'waiting_for_readiness' `
    -StartedAt $started | Out-Null
Get-AgentBridgeOpenClawLifecycleLease `
    -Path $env:AGENTBRIDGE_TEST_LEASE `
    -GatewayPort 18789 `
    -Now ([DateTimeOffset]::UtcNow.AddMinutes(1)) |
    ConvertTo-Json -Compress
""",
                lease_path=lease_path,
            )
            self.assertFalse(expired["active"])
            self.assertEqual("expired", expired["reason"])

    def test_startup_adoption_uses_original_bounded_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                """
Import-Module $env:AGENTBRIDGE_TEST_MODULE -Force
$started = [DateTimeOffset]'2026-09-07T00:00:00Z'
$checks = @{}
foreach ($age in @(-1, 179, 300, 599, 600, 900)) {
    $checks[[string]$age] = Test-AgentBridgeGatewayStartupWindow `
        -StartedAt $started -Now ($started.AddSeconds($age))
}
$checks['customExpired'] = Test-AgentBridgeGatewayStartupWindow `
    -StartedAt $started -Now ($started.AddSeconds(60)) -BudgetSeconds 60
$checks | ConvertTo-Json -Compress
""",
                lease_path=Path(directory) / "startup.json",
            )
            self.assertEqual(result, {"-1": False, "179": True, "300": True,
                                      "599": True, "600": False, "900": False,
                                      "customExpired": False})


if __name__ == "__main__":
    unittest.main()
