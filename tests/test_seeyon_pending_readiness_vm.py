"""Exercise real submission JavaScript without a browser or downstream network."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from agentbridge.adapters.seeyon_pending_execution import PENDING_ACTION_SCRIPT, PENDING_READINESS_SCRIPT


def test_observed_original_oa_entry_guard_and_new_scheduling_guard_in_node_vm():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for isolated JavaScript regression")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("pending_readiness_vm.mjs"))],
        input=json.dumps({"action": PENDING_ACTION_SCRIPT, "readiness": PENDING_READINESS_SCRIPT}),
        text=True, capture_output=True, timeout=10, check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence["lateBody"] == {"writesBeforeReady": 0, "writesAfterReady": 1, "checks": 1}
    assert evidence["reload"] == evidence["identity"] == {"entryCalls": 0, "writes": 0, "dispatch": "not_invoked"}
    assert evidence["originalValidation"] == {"checks": 1, "writes": 0}
