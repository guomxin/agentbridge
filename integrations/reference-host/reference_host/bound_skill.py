"""Pin the central skill binding to a private coordinate run and revalidate before each effect."""

import json


def pin_run(journal, request, capability):
    value = request.get("skillPin")
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    run = request["runId"]
    with journal:
        journal.execute("""CREATE TABLE IF NOT EXISTS coordinated_skill_scope (
            run TEXT PRIMARY KEY, skill_json TEXT NOT NULL)""")
        if not request.get("recoveryOnly"):
            journal.execute(
                "INSERT OR IGNORE INTO coordinated_skill_scope VALUES(?,?)",
                (run, serialized),
            )
        row = journal.execute(
            "SELECT skill_json FROM coordinated_skill_scope WHERE run=?", (run,)
        ).fetchone()
    if row is None:
        if request.get("recoveryOnly") and value is None:
            return None  # Existing unscoped runs keep their original binding.
        raise PermissionError("No original skill scope")
    if not request.get("recoveryOnly") and row[0] != serialized:
        raise PermissionError("Original skill scope changed")
    pin = json.loads(row[0])
    if pin is None:
        return None
    if (
        not isinstance(pin, dict)
        or set(pin)
        != {
            "skill_id",
            "profile",
            "expected_version",
            "binding_id",
            "source_id",
            "prepare",
        }
        or pin["prepare"] != capability
        or not all(
            isinstance(pin[k], str) and pin[k]
            for k in (
                "skill_id",
                "profile",
                "expected_version",
                "binding_id",
                "prepare",
            )
        )
    ):
        raise PermissionError("Invalid original skill scope")
    return pin


async def check_skill(driver):
    req = driver.req
    pin = req.get("checkedSkillPin")
    if pin is None:
        return
    meta = {
        **driver.host_context(),
        "io.agentbridge/task": req["task"],
        "agentbridge/skill": {"bindingId": pin["binding_id"]},
    }
    arguments = {k: pin[k] for k in ("skill_id", "profile", "expected_version")}
    if pin["source_id"] is not None:
        arguments["source_id"] = pin["source_id"]
    result = await driver.client("owner").call_tool(
        "agentbridge_skill_get", arguments, meta=meta, recovery_class="read"
    )
    payload = result.payload()
    contract = payload.get("execution") or {}
    if (
        result.is_error
        or payload.get("status") != "succeeded"
        or (
            payload.get("skill_id"),
            payload.get("profile"),
            payload.get("version"),
            payload.get("binding_id"),
        )
        != (pin["skill_id"], pin["profile"], pin["expected_version"], pin["binding_id"])
        or pin["prepare"] not in (contract.get("capabilities") or [])
        or not set(contract.get("effects") or []).intersection(
            {"reversible_write", "controlled_write"}
        )
        or (".batch." in pin["prepare"] and contract.get("allow_batch") is not True)
    ):
        raise PermissionError(
            "Current central skill scope no longer permits this preparation"
        )
