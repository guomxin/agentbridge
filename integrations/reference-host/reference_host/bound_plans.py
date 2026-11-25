"""Issue one registered plan through the original coordinator and monitor its private card."""

import hashlib
import json
import sqlite3
from pathlib import Path


async def begin_write_plan(driver, local_id, proposal, governed):
    req = driver.req
    task = req["task"]
    sink = governed[req["tool"]]["prepareCapability"]
    steps = proposal.get("steps", [])
    prepares = {v["prepareCapability"] for v in governed.values()}
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(step, dict) for step in steps)
        or steps[-1].get("capabilityName") != sink
        or sum(step.get("capabilityName") in prepares for step in steps) != 1
    ):
        raise PermissionError("A single final governed prepare is required")
    digest = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    ledger = Path(req["userDir"]) / "plans.sqlite3"
    # The adapter records its non-replayable admission before entering this private helper.
    with sqlite3.connect(ledger) as db:
        original = db.execute(
            "SELECT task,digest,plan FROM plans WHERE run=?", (req["runId"],)
        ).fetchone()
    if original != (task["taskId"], digest, None):
        raise PermissionError("Original plan admission is unavailable")
    with driver.journal:
        driver.journal.execute(
            "INSERT INTO write_plan_scope VALUES(?, '')", (req["runId"],)
        )
    lease = await driver.renew_coordinator_lease(local_id)
    meta = {
        **driver.host_context(),
        "io.agentbridge/task": {
            **task,
            "coordinatorLeaseVersion": str(lease["version"]),
        },
    }
    if req["checkedSkillPin"]:
        meta["agentbridge/skill"] = {"bindingId": req["checkedSkillPin"]["binding_id"]}
    answer = await driver.client("owner").call_tool(
        "agentbridge_task_plan_prepare",
        {**proposal, "idempotency_key": driver.implementation.name + ":" + req["runId"]},
        meta=meta,
        recovery_class="unsafe",
    )
    data = answer.payload()
    plan = data.get("plan", {})
    ref = plan.get("planId")
    summary = plan.get("riskSummary") or {}
    if (
        answer.is_error
        or not isinstance(ref, str)
        or not ref
        or plan.get("taskId") != task["taskId"]
        or summary.get("writeSinkCount") != 1
        or summary.get("writeSinkCapability") != sink
    ):
        raise RuntimeError("The original central plan has no matching receipt")
    with sqlite3.connect(ledger) as db:
        recorded = db.execute(
            "UPDATE plans SET plan=? WHERE run=? AND task=? AND digest=? AND plan IS NULL",
            (ref, req["runId"], task["taskId"], digest),
        ).rowcount
        if recorded != 1:
            raise RuntimeError("Original plan receipt changed")
    with driver.journal:
        driver.journal.execute(
            "UPDATE write_plan_scope SET plan=? WHERE run=? AND plan=''",
            (ref, req["runId"]),
        )
    req["boundPlanId"] = ref
    await driver.process_tool_result(local_id, answer)


async def current_plan_result(driver, local_id, result):
    """Only the current step may be observed on the parent. Completed reads never finish a waiting write."""
    from dataclasses import replace

    req = driver.req
    plan_id = req["boundPlanId"]
    observed = await driver.client("owner").call_tool(
        "agentbridge_task_plan_get",
        {"plan_id": plan_id},
        meta=driver.host_context(),
        recovery_class="read",
    )
    plan = observed.payload().get("plan") or {}
    if (
        observed.is_error
        or plan.get("planId") != plan_id
        or plan.get("taskId") != req["task"]["taskId"]
    ):
        raise PermissionError("Original plan status is unavailable")
    phase = {
        "failed": "failed",
        "canceled": "canceled",
        "outcome_unknown": "unknown",
    }.get(plan.get("state"))
    if phase or result.is_error:
        phase = phase or "unknown"
        driver.state.finish_task(local_id, status=phase)
        with driver.journal:
            driver.journal.execute(
                "UPDATE coordinated SET phase=? WHERE run=?", (phase, req["runId"])
            )
        return None
    steps = plan.get("steps") or []
    if plan.get("state") == "waiting_user":
        card = (
            result.private_meta.get("io.agentbridge/interaction")
            or result.payload().get("interaction")
            or {}
        )
        step = next(
            (s for s in steps if s["stepKey"] == plan.get("currentStepKey")), {}
        )
        if (
            not step.get("interactionId")
            or step["interactionId"] != card.get("interactionId")
            or step.get("state") != "waiting_user"
        ):
            raise PermissionError("Current plan card is unavailable")
        data = {
            "status": "requires_user_action",
            "interaction": {"interactionId": step["interactionId"]},
        }
    elif (
        plan.get("state") == "succeeded"
        and steps
        and steps[-1].get("operationId")
        and steps[-1]["state"] == "succeeded"
        and steps[-1].get("capabilityName") == req["expectedPrepare"]
    ):
        data = {"status": "succeeded", "operationId": steps[-1]["operationId"]}
        return replace(result, structured=data, content=[], private_meta={})
    else:
        raise RuntimeError("Plan has no confirmed completion or current card")
    return replace(
        result,
        structured=data,
        content=[],
        private_meta={"io.agentbridge/interaction": card},
    )
