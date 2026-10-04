import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Legacy Windows PowerShell entrypoint; outside macOS/Linux maintenance")
class OpenClawRestartPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the same PowerShell 7 runtime required by the release entrypoints.
        # Windows also has 5.1 installed; choosing it first made CI use a different host.
        cls.shell = shutil.which("pwsh")
        if not cls.shell:
            raise unittest.SkipTest("PowerShell 7 is required")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        plugin = self.root / "integrations/openclaw-agentbridge"
        (plugin / "lib").mkdir(parents=True)
        for name, text in {
            "index.js": "export default {};",
            "package.json": '{"version":"1.0.0"}',
            "openclaw.plugin.json": '{"id":"test"}',
            "lib/tools.json": '{"tools":[]}',
            "lib/module.js": "export const value=1;",
        }.items():
            (plugin / name).write_text(text, encoding="utf-8")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        (self.root / "gateway.cmd").write_text("node gateway", encoding="utf-8")

    def run_policy(self, script):
        preamble = """
$ErrorActionPreference = 'Stop'
Import-Module $env:POLICY_MODULE -Force
function Snapshot {
    Get-AgentBridgeOpenClawInputs -RepoRoot $env:POLICY_ROOT `
        -GatewayLauncher (Join-Path $env:POLICY_ROOT 'gateway.cmd') `
        -ConfigPath (Join-Path $env:POLICY_ROOT 'config.json')
}
$before = Snapshot
$started = '2026-09-09T01:00:00Z'
$baseline = [pscustomobject]@{
    schemaVersion = 'agentbridge.openclaw-inputs.v1'
    gatewayProcessId = 123
    gatewayStartedAt = $started
    inputs = $before
}
function Decide($snapshot) {
    Get-AgentBridgeOpenClawRestartDecision -Inputs $snapshot -Baseline $baseline `
        -GatewayProcessId 123 -GatewayStartedAt $started -Ready $true
}
"""
        result = subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-Command", preamble + script],
            env={**os.environ, "POLICY_ROOT": str(self.root),
                 "POLICY_MODULE": str(ROOT / "scripts/AgentBridgeOpenClawRestartPolicy.psm1")},
            stdin=subprocess.DEVNULL,
            # Shared Windows runners can be heavily contended under pytest-xdist.
            # This is a correctness check, not a 40-second performance contract.
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_relocated_checkout_and_plugin_newlines_do_not_restart(self):
        result = self.run_policy("""
$copy = Join-Path $env:POLICY_ROOT 'release'
New-Item -ItemType Directory $copy | Out-Null
Copy-Item (Join-Path $env:POLICY_ROOT 'integrations') $copy -Recurse
$p = Join-Path $copy 'integrations/openclaw-agentbridge/index.js'
[IO.File]::WriteAllText($p, ([IO.File]::ReadAllText($p)).Replace(';', ";`r`n"))
$original = Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/index.js'
[IO.File]::WriteAllText($original, ([IO.File]::ReadAllText($original)).Replace(';', ";`n"))
$baseline.inputs = Snapshot
$after = Get-AgentBridgeOpenClawInputs -RepoRoot $copy `
    -GatewayLauncher (Join-Path $env:POLICY_ROOT 'gateway.cmd') `
    -ConfigPath (Join-Path $env:POLICY_ROOT 'config.json')
[pscustomobject]@{decision=(Decide $after); equal=($baseline.inputs.fingerprint -eq $after.fingerprint)} | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertFalse(result["decision"]["required"])
        self.assertTrue(result["equal"])

    def test_legacy_baseline_is_verified_before_newline_conversion(self):
        result = self.run_policy("""
$p = Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/index.js'
[IO.File]::WriteAllText($p, "export default {};`r`n")
$baseline.inputs = Snapshot
$baseline.inputs.PSObject.Properties.Remove('algorithm')
$copy = Join-Path $env:POLICY_ROOT 'release'
New-Item -ItemType Directory $copy | Out-Null
Copy-Item (Join-Path $env:POLICY_ROOT 'integrations') $copy -Recurse
[IO.File]::WriteAllText((Join-Path $copy 'integrations/openclaw-agentbridge/index.js'), "export default {};`n")
$after = Get-AgentBridgeOpenClawInputs -RepoRoot $copy `
    -GatewayLauncher (Join-Path $env:POLICY_ROOT 'gateway.cmd') `
    -ConfigPath (Join-Path $env:POLICY_ROOT 'config.json')
$verified = Decide $after
[IO.File]::WriteAllText($p, 'changed since certification')
$unverified = Decide $after
[pscustomobject]@{verified=$verified; unverified=$unverified} | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertFalse(result["verified"]["required"])
        self.assertTrue(result["unverified"]["required"])
        self.assertEqual(result["unverified"]["reason"], "baseline_invalid")

    def test_legacy_exact_bytes_work_even_after_old_directory_disappears(self):
        result = self.run_policy("""
$baseline.inputs.PSObject.Properties.Remove('algorithm')
foreach ($f in $baseline.inputs.files) { $f.path = Join-Path $env:POLICY_ROOT 'missing-old-path' }
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertFalse(result["required"])

    def test_host_newlines_and_unknown_binary_assets_remain_exact(self):
        result = self.run_policy("""
$p = Join-Path $env:POLICY_ROOT 'gateway.cmd'
$b = Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/lib/asset.bin'
[IO.File]::WriteAllText($p, "node gateway`r`n")
[IO.File]::WriteAllBytes($b, [byte[]]@(0,13,10,255))
$baseline.inputs = Snapshot
[IO.File]::WriteAllText($p, "node gateway`n")
[IO.File]::WriteAllBytes($b, [byte[]]@(0,10,255))
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertTrue(result["required"])
        self.assertEqual(result["changedInputs"], ["host/launcher", "plugin/lib/asset.bin"])

    def test_typed_json_baseline_keeps_precision_in_restart_decision(self):
        result = self.run_policy("""
$precise = [DateTimeOffset]::Parse('2026-09-09T20:01:23.1234567+08:00')
$typed = [pscustomobject]@{schemaVersion='agentbridge.openclaw-inputs.v1'; gatewayProcessId=123;
    gatewayStartedAt=$precise.LocalDateTime; inputs=$before}
Get-AgentBridgeOpenClawRestartDecision -Inputs (Snapshot) -Baseline $typed `
    -GatewayProcessId 123 -GatewayStartedAt $precise.ToString('o') -Ready $true | ConvertTo-Json -Compress
""")
        self.assertFalse(result["required"])

    def test_baseline_preserves_typed_datetime_fractional_seconds(self):
        result = self.run_policy("""
$precise = [DateTimeOffset]::Parse('2026-09-09T20:01:23.1234567+08:00')
$path = Join-Path $env:POLICY_ROOT 'typed-baseline.json'
Save-AgentBridgeOpenClawBaseline -Before $before -After (Snapshot) `
    -GatewayProcessId 123 -GatewayStartedAt $precise.LocalDateTime -Path $path
$loaded = Get-Content $path -Raw | ConvertFrom-Json
[pscustomobject]@{exact=([DateTimeOffset]$loaded.gatewayStartedAt -eq $precise)} | ConvertTo-Json -Compress
""")
        self.assertTrue(result["exact"])

    def test_central_workspace_docs_and_tests_do_not_invalidate(self):
        result = self.run_policy("""
foreach ($name in @('bscli/workspace/index.html', 'bscli/service.py',
    'docs/release.md', 'integrations/openclaw-agentbridge/README.md',
    'integrations/openclaw-agentbridge/test/plugin.test.js')) {
    $p = Join-Path $env:POLICY_ROOT $name
    New-Item -ItemType Directory -Path (Split-Path $p -Parent) -Force | Out-Null
    Set-Content -LiteralPath $p -Value 'changed'
}
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertFalse(result["required"])
        self.assertEqual(result["reason"], "inputs_unchanged")

    def test_same_version_source_and_catalogue_change_require_restart(self):
        result = self.run_policy("""
Set-Content (Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/index.js') 'changed'
Set-Content (Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/lib/tools.json') '{"tools":[1]}'
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertTrue(result["required"])
        self.assertEqual(result["changedInputs"], ["plugin/index.js", "plugin/lib/tools.json"])

    def test_deletion_addition_and_dependency_lock_are_detected(self):
        result = self.run_policy("""
$p = Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge'
Remove-Item -LiteralPath (Join-Path $p 'lib/module.js')
Set-Content (Join-Path $p 'lib/new.js') 'new'
Set-Content (Join-Path $p 'package-lock.json') '{}'
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertEqual(result["changedInputs"], [
            "plugin/lib/module.js", "plugin/lib/new.js", "plugin/package-lock.json"])

    def test_config_launcher_and_environment_changes_are_detected(self):
        result = self.run_policy("""
Set-Content (Join-Path $env:POLICY_ROOT 'config.json') '{"changed":true}'
Set-Content (Join-Path $env:POLICY_ROOT 'gateway.cmd') 'changed'
Set-Content (Join-Path $env:POLICY_ROOT '.env') 'TEST_SECRET=never_emit_value'
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertTrue(result["required"])
        self.assertIn("host/config", result["changedInputs"])
        self.assertIn("host/launcher", result["changedInputs"])
        self.assertIn("workspace/env", result["changedInputs"])
        self.assertNotIn("never_emit_value", json.dumps(result))

    def test_missing_invalid_or_stale_baseline_never_certifies_current_process(self):
        result = self.run_policy("""
$common = @{Inputs=$before; GatewayProcessId=123; GatewayStartedAt=$started; Ready=$true}
$missing = Get-AgentBridgeOpenClawRestartDecision @common
$invalid = Get-AgentBridgeOpenClawRestartDecision @common -Baseline @{}
$baseline.gatewayStartedAt = '2026-09-08T01:00:00Z'
$reusedPid = Get-AgentBridgeOpenClawRestartDecision @common -Baseline $baseline
@($missing, $invalid, $reusedPid) | ConvertTo-Json -Compress
""")
        self.assertEqual([x["reason"] for x in result], [
            "baseline_missing", "baseline_invalid", "gateway_process_changed"])
        self.assertTrue(all(x["required"] for x in result))

    def test_force_and_unhealthy_runtime_require_restart(self):
        result = self.run_policy("""
$common = @{Inputs=$before; Baseline=$baseline; GatewayProcessId=123; GatewayStartedAt=$started}
@((Get-AgentBridgeOpenClawRestartDecision @common -Ready $true -Force),
  (Get-AgentBridgeOpenClawRestartDecision @common -Ready $false)) | ConvertTo-Json -Compress
""")
        self.assertEqual([x["reason"] for x in result], ["forced", "gateway_not_ready"])

    def test_baseline_roundtrip_and_startup_mutation_rejection(self):
        result = self.run_policy("""
$path = Join-Path $env:POLICY_ROOT 'baseline.json'
Save-AgentBridgeOpenClawBaseline -Before $before -After (Snapshot) `
    -GatewayProcessId 123 -GatewayStartedAt $started -Path $path
$baseline = Get-Content $path -Raw | ConvertFrom-Json
$unchanged = Decide (Snapshot)
$original = Get-Content $path -Raw
Set-Content (Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/lib/module.js') 'changed'
$rejected = $false
try {
    Save-AgentBridgeOpenClawBaseline -Before $before -After (Snapshot) `
        -GatewayProcessId 124 -GatewayStartedAt $started -Path $path
} catch { $rejected = $true }
[pscustomobject]@{unchanged=$unchanged; rejected=$rejected;
    preserved=($original -eq (Get-Content $path -Raw))} | ConvertTo-Json -Compress
""")
        self.assertFalse(result["unchanged"]["required"])
        self.assertTrue(result["rejected"])
        self.assertTrue(result["preserved"])

    def test_manifest_changes_are_runtime_changes(self):
        result = self.run_policy("""
Set-Content (Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/package.json') '{"version":"2"}'
Set-Content (Join-Path $env:POLICY_ROOT 'integrations/openclaw-agentbridge/openclaw.plugin.json') '{}'
Decide (Snapshot) | ConvertTo-Json -Compress
""")
        self.assertEqual(result["changedInputs"], ["plugin/openclaw.plugin.json", "plugin/package.json"])

    def test_failed_warmup_retries_without_losing_acceptance(self):
        result = self.run_policy("""
$pending = Join-Path $env:POLICY_ROOT 'warmup.pending'
Set-Content $pending 'pending'
$plan = { [pscustomobject]@{required=$false; gatewayProcessId=123; fingerprint='abc'} }
$failed = $false
try {
    Complete-AgentBridgeOpenClawWarmup -PendingPath $pending -GetPlan $plan -Warmup {
        throw 'model timeout'
    }
} catch { $failed = $true }
$retained = Test-Path $pending
$retried = Complete-AgentBridgeOpenClawWarmup -PendingPath $pending -GetPlan $plan -Warmup {
    [pscustomobject]@{status='succeeded'}
}
$skipped = Complete-AgentBridgeOpenClawWarmup -PendingPath $pending -GetPlan { throw 'unexpected probe' } `
    -Warmup { throw 'unexpected model call' }
[pscustomobject]@{failed=$failed; retained=$retained; retried=$retried.status;
    skipped=$skipped.status; cleared=(-not (Test-Path $pending))} | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {"failed": True, "retained": True, "retried": "succeeded",
                                  "skipped": "skipped", "cleared": True})

    def test_gateway_change_during_warmup_keeps_acceptance_pending(self):
        result = self.run_policy("""
$pending = Join-Path $env:POLICY_ROOT 'warmup.pending'
Set-Content $pending 'pending'
$state = @{pid=123}
$failed = $false
try {
    Complete-AgentBridgeOpenClawWarmup -PendingPath $pending -GetPlan {
        [pscustomobject]@{required=$false; gatewayProcessId=$state.pid; fingerprint='abc'}
    } -Warmup {
        $state.pid = 124
        [pscustomobject]@{status='succeeded'}
    }
} catch { $failed = $true }
[pscustomobject]@{failed=$failed; retained=(Test-Path $pending)} | ConvertTo-Json -Compress
""")
        self.assertEqual(result, {"failed": True, "retained": True})


if __name__ == "__main__":
    unittest.main()
