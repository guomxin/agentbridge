from __future__ import annotations

from pathlib import Path
from typing import Any


_SCRIPT_ROOT = Path(__file__).with_name("seeyon_page_scripts")

_SEEYON_ACTION_SCRIPTS = {
    "ContinueSubmit": {
        "script_name": "seeyon.continue_submit.v1",
        "filename": "continue_submit.js",
        "outcome_key": "__agentbridgeContinueSubmitLast",
    },
    "SaveDraft": {
        "script_name": "seeyon.launch_save_draft.v1",
        "filename": "launch_save_draft.js",
        "outcome_key": "__agentbridgeLaunchSaveDraftLast",
    },
}


def load_seeyon_action_page_script(action: str) -> dict[str, Any]:
    spec = _SEEYON_ACTION_SCRIPTS.get(action)
    if spec is None:
        raise ValueError(f"no Seeyon page script is registered for action: {action}")
    script_path = _SCRIPT_ROOT / str(spec["filename"])
    return {
        "script_name": spec["script_name"],
        "script_source": script_path.read_text(encoding="utf-8"),
        "outcome_key": spec.get("outcome_key", ""),
    }
