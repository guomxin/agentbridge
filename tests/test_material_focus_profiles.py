"""Review layouts cover the current closed action registry, not historical counts."""
import json
from pathlib import Path
import shutil
import subprocess

from agentbridge.core.business_draft_profiles import PROFILES


def test_every_registered_field_action_has_a_source_focus_and_review_layout():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    assert node, "Node is required by the existing full validation environment"
    result = subprocess.run([node, "--input-type=module", "-e",
        "import {REVIEW_DESIGNS,REVIEW_FOCUS} from './agentbridge/auth/static/business_draft.mjs';"
        "console.log(JSON.stringify({designs:Object.keys(REVIEW_DESIGNS),focus:Object.keys(REVIEW_FOCUS)}));"],
        cwd=root, text=True, capture_output=True, check=True)
    layouts = json.loads(result.stdout)
    kinds = {profile.review_kind for profile in PROFILES}
    assert set(layouts["designs"]) == kinds
    assert set(layouts["focus"]) == kinds
    assert all(profile.fields and profile.review_kind in layouts["focus"] for profile in PROFILES)
