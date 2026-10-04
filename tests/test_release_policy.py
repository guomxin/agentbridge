"""Validate the actual release declaration as well as transaction fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from scripts.ci_validation_plan import git


def test_checked_in_release_policy_has_exact_existing_predecessors():
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / 'deploy/release-policy.json').read_text(encoding='utf-8'))
    assert policy['schemaVersion'] == 'agentbridge.release-policy.v1'
    assert policy['dataCompatibility'] in {'no-migration', 'reviewed-schema-transition'}
    if policy['dataCompatibility'] == 'reviewed-schema-transition':
        assert policy['schemaTransitions']
        for transition in policy['schemaTransitions'].values():
            assert re.fullmatch('[0-9a-f]{64}', transition['beforeSha256'])
            assert re.fullmatch('[0-9a-f]{64}', transition['afterSha256'])
    assert isinstance(policy['reason'], str) and policy['reason'].strip()
    predecessors = policy['compatibleFrom']
    assert isinstance(predecessors, list) and predecessors
    assert len(predecessors) == len(set(predecessors))
    for predecessor in predecessors:
        assert re.fullmatch('[0-9a-f]{12}', predecessor)
        commit = git(root, 'rev-parse', '--verify', predecessor + '^{commit}')
        assert git(root, 'merge-base', 'HEAD', commit) == commit


@pytest.mark.skipif(os.name != "nt", reason="Legacy Windows PowerShell entrypoint; outside macOS/Linux maintenance")
@pytest.mark.parametrize('current,candidate,mode,compatibility,allowed', [
    ('111111111111', '222222222222', '', 'no-migration', True),
    ('333333333333', '222222222222', '', 'no-migration', False),
    ('111111111111', '222222222222', '', 'migration-required', False),
    ('222222222222', '222222222222', '-ResumeAcceptance', 'no-migration', True),
    ('111111111111', '222222222222', '-ResumeAcceptance', 'no-migration', False),
    ('unknown', '222222222222', '', 'no-migration', False),
])
def test_preflight_checks_real_powershell_contract(tmp_path, current, candidate, mode, compatibility, allowed):
    shell = shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell is required for release entrypoint tests')
    root = Path(__file__).resolve().parents[1]
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'schemaVersion': 'agentbridge.release-policy.v1',
                                 'compatibleFrom': ['111111111111'],
                                 'dataCompatibility': compatibility}))
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = ("$ErrorActionPreference='Stop'; Import-Module "
               + quote(root / 'scripts/AgentBridgeReleasePreflight.psm1')
               + '; Assert-AgentBridgeReleaseCompatibility -CurrentRelease ' + quote(current)
               + ' -CandidateRelease ' + quote(candidate) + ' -PolicyPath ' + quote(policy)
               + ' ' + mode + ' | ConvertTo-Json -Compress')
    result = subprocess.run([shell, '-NoProfile', '-Command', command], capture_output=True, text=True, timeout=20)
    assert (result.returncode == 0) == allowed, result.stdout + result.stderr
    if allowed:
        assert json.loads(result.stdout)['currentRelease'] == current


def test_preflight_runs_before_plan_output_and_expensive_validation():
    root = Path(__file__).resolve().parents[1]
    for filename in ['Publish-AgentBridge.ps1', 'Deploy-AgentBridge.ps1']:
        source = (root / 'scripts' / filename).read_text(encoding='utf-8')
        preflight = source.index('$releasePreflight = Get-AgentBridgeReleasePreflight')
        assert preflight < source.index('if ($PlanOnly)')
        validation_guard = (
            'if (-not $reuseValidation)'
            if filename == 'Publish-AgentBridge.ps1'
            else 'if (-not $SkipValidation)'
        )
        assert preflight < source.index(validation_guard)
    # Early checks are advisory snapshots, not replacements for the locked check.
    runner = (root / 'scripts/agentbridge_release.py').read_text(encoding='utf-8')
    assert 'previous not in policy["compatibleFrom"]' in runner
