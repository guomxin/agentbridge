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
