from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.parse import urlparse

from agentbridge.adapters.seeyon_business_trip_submit import (
    prepare_business_trip_submission,
)
from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from agentbridge.adapters.seeyon_leave import prepare_leave_draft
from agentbridge.adapters.seeyon_leave_submit import prepare_leave_submission
from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.core.config import ConfigStore
from agentbridge.core.session_secrets import SessionStateStore
from agentbridge.core.sessions import SessionRegistry


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate governed OA write preparation against a real central session while "
            "blocking collaboration writes and never clicking save or send."
        )
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--user-subject")
    parser.add_argument(
        "--workflow",
        action="append",
        choices=["business-trip-submit", "leave-draft", "leave-submit"],
    )
    args = parser.parse_args()

    home = Path(args.home).resolve()
    workflows = args.workflow or ["business-trip-submit", "leave-draft", "leave-submit"]
    profile = ConfigStore(home).load_system("oa")
    registry = SessionRegistry(home / "agentbridge.db", home / "profiles")
    sessions = registry.list_active(system_id="oa")
    if args.user_subject:
        sessions = [item for item in sessions if item["user_subject"] == args.user_subject]
    if len(sessions) != 1:
        raise RuntimeError("exactly one matching active OA session is required")

    session = sessions[0]
    state = SessionStateStore(home / "session-secrets").load(session["session_id"])
    if state is None:
        raise RuntimeError("the active OA session has no encrypted browser state")

    adapter = SeeyonCentralAdapter(base_url=profile.base_url)
    blocked_writes: list[dict] = []
    results: list[dict] = []
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
              const control = event.target?.closest?.('#saveDraft_a,#sendId_a');
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
        for workflow in workflows:
            prepared = _prepare(workflow, adapter, worker)
            clicks = page.evaluate("() => window.__agentbridgeWriteControlClicks || []")
            if clicks:
                raise RuntimeError(f"write control was clicked during preflight: {clicks}")
            results.append(_safe_result(workflow, prepared))

    if blocked_writes:
        raise RuntimeError(
            "an OA collaboration write request was attempted and blocked during preflight"
        )
    print(
        json.dumps(
            {
                "schema_version": "agentbridge.oa_write_preflight.v1",
                "status": "succeeded",
                "session": {
                    "user_subject": session["user_subject"],
                    "state": session["state"],
                },
                "results": results,
                "safety": {
                    "write_controls_clicked": 0,
                    "collaboration_write_requests": 0,
                    "drafts_saved": 0,
                    "workflows_submitted": 0,
                    "exact_inputs_included": False,
                    "cookies_included": False,
                },
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _prepare(workflow: str, adapter, worker) -> dict:
    base = (datetime.now() + timedelta(days=2)).replace(
        hour=9,
        minute=0,
        second=0,
        microsecond=0,
    )
    if workflow == "business-trip-submit":
        return prepare_business_trip_submission(
            adapter,
            worker,
            {
                "start_time": base.strftime("%Y-%m-%d %H:%M"),
                "end_time": base.replace(hour=18).strftime("%Y-%m-%d %H:%M"),
                "travel_mode": "火车",
                "origin": "济南",
                "destination": "青岛",
                "reason": "AgentBridge 非提交契约验证",
                "has_direct_supervisor": False,
            },
        )
    if workflow == "leave-draft":
        leave_start = base + timedelta(days=1)
        return prepare_leave_draft(
            adapter,
            worker,
            {
                "leave_type": "事假",
                "start_time": leave_start.strftime("%Y-%m-%d %H:%M"),
                "end_time": leave_start.replace(hour=12).strftime("%Y-%m-%d %H:%M"),
                "reason": "AgentBridge 非提交契约验证",
                "has_direct_supervisor": False,
            },
        )
    if workflow == "leave-submit":
        leave_start = base + timedelta(days=1)
        return prepare_leave_submission(
            adapter,
            worker,
            {
                "leave_type": "事假",
                "start_time": leave_start.strftime("%Y-%m-%d %H:%M"),
                "end_time": leave_start.replace(hour=12).strftime("%Y-%m-%d %H:%M"),
                "reason": "AgentBridge 非提交契约验证",
                "has_direct_supervisor": False,
            },
        )
    raise ValueError(f"unsupported preflight workflow: {workflow}")


def _safe_result(workflow: str, prepared: dict) -> dict:
    plan = prepared.get("plan") if isinstance(prepared.get("plan"), dict) else {}
    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    contract = (
        plan.get("form_contract") if isinstance(plan.get("form_contract"), dict) else {}
    )
    preconditions = (
        plan.get("preconditions") if isinstance(plan.get("preconditions"), dict) else {}
    )
    return {
        "workflow": workflow,
        "business_intent": plan.get("business_intent"),
        "template": {
            "title": target.get("template_title"),
            "template_id": target.get("template_id"),
            "form_app_id": target.get("form_app_id"),
        },
        "contract": {
            "version": contract.get("version"),
            "fingerprint": contract.get("fingerprint"),
        },
        "preconditions": {
            key: value
            for key, value in preconditions.items()
            if isinstance(value, bool)
        },
        "expected_effect": plan.get("expected_effect"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
