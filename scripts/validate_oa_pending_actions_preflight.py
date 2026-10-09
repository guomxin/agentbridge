from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from agentbridge.adapters.seeyon_missed_punch import prepare_missed_punch_approval
from agentbridge.adapters.seeyon_pending_actions import (
    prepare_attendance_confirmation,
    prepare_business_trip_approval,
    prepare_efficiency_data_approval,
    prepare_intellectual_property_declaration_approval,
    prepare_labor_contract_renewal_approval,
    prepare_leave_approval,
    prepare_overtime_approval,
    prepare_resignation_approval,
    prepare_work_handover_approval,
    prepare_flight_application_approval,
    prepare_standard_collaboration_approval,
    prepare_travel_expense_approval,
    prepare_weekly_report_acknowledgement,
)
from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.core.config import ConfigStore
from agentbridge.core.session_secrets import SessionStateStore
from agentbridge.core.sessions import SessionRegistry


_PREPARE_FUNCTIONS = {
    "attendance_confirmation": prepare_attendance_confirmation,
    "business_trip": prepare_business_trip_approval,
    "efficiency_data": prepare_efficiency_data_approval,
    "intellectual_property_declaration": (
        prepare_intellectual_property_declaration_approval
    ),
    "labor_contract_renewal": prepare_labor_contract_renewal_approval,
    "leave": prepare_leave_approval,
    "overtime": prepare_overtime_approval,
    "resignation": prepare_resignation_approval,
    "work_handover": prepare_work_handover_approval,
    "flight_application": prepare_flight_application_approval,
    "missed_punch_approval": prepare_missed_punch_approval,
    "travel_expense": prepare_travel_expense_approval,
    "weekly_report": prepare_weekly_report_acknowledgement,
    "standard_collaboration": prepare_standard_collaboration_approval,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate registered OA pending-action profiles without writing."
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--user-subject")
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        help="Registered profile and opaque affair ID as profile:affair_id.",
    )
    args = parser.parse_args()

    targets = [_parse_target(value) for value in args.target]
    home = Path(args.home).resolve()
    profile = ConfigStore(home).load_system("oa")
    registry = SessionRegistry(home / "agentbridge.db", home / "profiles")
    sessions = registry.list_active(system_id="oa")
    if args.user_subject:
        sessions = [
            item for item in sessions if item["user_subject"] == args.user_subject
        ]
    if len(sessions) != 1:
        raise RuntimeError("exactly one matching active OA session is required")

    session = sessions[0]
    state = SessionStateStore(home / "session-secrets").load(session["session_id"])
    if state is None:
        raise RuntimeError("the active OA session has no encrypted browser state")

    adapter = SeeyonCentralAdapter(base_url=profile.base_url)
    results = []
    blocked_writes: list[dict] = []
    with CentralBrowserWorker(
        profile_path=session["profile_path"],
        allowed_origins={adapter.origin},
        headless=True,
    ) as worker:
        worker.restore_session_state(state)
        page = worker.page
        page.add_init_script(
            r"""
            window.__agentbridgeWriteControlClicks = [];
            document.addEventListener('click', (event) => {
              const control = event.target?.closest?.(
                '#submit_a,#dealSubmit_a,#saveDraft_a,#sendId_a'
              );
              if (control) window.__agentbridgeWriteControlClicks.push(control.id);
            }, true);
            """
        )

        def guard_route(route) -> None:
            request = route.request
            parsed = urlparse(str(request.url or ""))
            if (
                str(request.method or "").upper() == "POST"
                and parsed.path.endswith("/collaboration/collaboration.do")
            ):
                blocked_writes.append(
                    {
                        "method": "POST",
                        "endpoint": "/seeyon/collaboration/collaboration.do",
                    }
                )
                route.abort()
                return
            route.continue_()

        worker._context.route("**/*", guard_route)
        for profile_name, affair_id in targets:
            prepared = _PREPARE_FUNCTIONS[profile_name](
                adapter,
                worker,
                {"affair_id": affair_id, "opinion": "preflight-only"},
            )
            clicks = page.evaluate("() => window.__agentbridgeWriteControlClicks || []")
            if clicks:
                raise RuntimeError(f"write control was clicked during preflight: {clicks}")
            plan = prepared["plan"]
            results.append(
                {
                    "profile": profile_name,
                    "affair_id": affair_id,
                    "business_intent": plan["business_intent"],
                    "action_kind": plan["action_contract"]["action_kind"],
                    "contract_version": plan["action_contract"]["version"],
                    "matched": True,
                }
            )
    if blocked_writes:
        raise RuntimeError(
            "an OA collaboration write request was attempted and blocked during preflight"
        )

    print(
        json.dumps(
            {
                "schema_version": "agentbridge.oa_pending_action_preflight.v1",
                "count": len(results),
                "items": results,
                "safety": {
                    "write_controls_clicked": 0,
                    "collaboration_write_requests": 0,
                    "authorizations_created": 0,
                    "business_values_included": False,
                },
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_target(value: str) -> tuple[str, str]:
    profile_name, separator, affair_id = str(value or "").partition(":")
    profile_name = profile_name.strip()
    affair_id = affair_id.strip()
    if not separator or profile_name not in _PREPARE_FUNCTIONS or not affair_id:
        choices = ", ".join(sorted(_PREPARE_FUNCTIONS))
        raise ValueError(f"target must use profile:affair_id; profiles: {choices}")
    if len(affair_id) > 256 or any(ord(character) < 32 for character in affair_id):
        raise ValueError("target affair_id is invalid")
    return profile_name, affair_id


if __name__ == "__main__":
    raise SystemExit(main())
