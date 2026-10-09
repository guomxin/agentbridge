from __future__ import annotations

import time
from typing import Type


_MESSAGE_BUTTON = '[id$="ok_msg_btn_first"]:visible'
_BLOCKING_OVERLAYS = (
    ".cap4-loading:visible",
    ".mask.mask_msg:visible",
)


def wait_for_cap4_interactive(
    page,
    frame,
    *,
    error_type: Type[Exception] = RuntimeError,
    context: str = "The OA CAP4 form",
    timeout_seconds: float = 8,
    settle_polls: int = 6,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 1)
    clear_polls = 0
    while time.monotonic() < deadline:
        handled_notice = False
        blockers_visible = False
        for root in (page, frame):
            buttons = root.locator(_MESSAGE_BUTTON)
            if buttons.count():
                buttons.first.click(timeout=3000)
                handled_notice = True
            blockers_visible = blockers_visible or any(
                root.locator(selector).count() for selector in _BLOCKING_OVERLAYS
            )

        if handled_notice or blockers_visible:
            clear_polls = 0
        else:
            clear_polls += 1
            if clear_polls >= max(settle_polls, 1):
                return
        page.wait_for_timeout(100)

    raise error_type(f"{context} did not become interactive in time.")
