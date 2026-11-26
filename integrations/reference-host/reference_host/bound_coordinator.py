"""Trusted pipe entry for one prebound Workspace task. No listener or model access."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import urlsplit

from .bound_skill import check_skill, pin_run
from .bound_commands import PREPARE, QUERY, command_snapshot, finish_query, query_result
from .host_profile import CoordinatorImplementation
from .identity import IdentityConfig, IdentityRegistry
from .runtime_collector import ReferenceHostRuntimeCollector
from .state import ReferenceHostState
from .task_driver import ReferenceTaskDriver

IMPLEMENTATION = CoordinatorImplementation(
    "nanobot", "0.3.0", ("web_private", "telegram_private")
)


PI_IMPLEMENTATION = CoordinatorImplementation("pi", "0.1.7", ("web_private", "telegram_private"))


class PersonalIdentity(IdentityConfig):
    def token(self) -> str:
        fd = os.open(self.token_env, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
            ):
                raise PermissionError("Unsafe personal credential file")
            value = handle.read(16385).strip()
        if not value or len(value) > 16384:
            raise ValueError("Invalid personal credential")
        return value


class BoundDriver(ReferenceTaskDriver):
    def __init__(self, req, journal, **kwargs):
        super().__init__(**kwargs)
        self.req = req
        self.journal = journal

    async def begin_governed(self, local_id, tool, arguments, governed):
        # The coordinator reuses the original prepare and its stable idempotency key after login.
        schema = self.tool("owner", tool).get("inputSchema") or {}
        if "idempotency_key" in (schema.get("properties") or {}):
            arguments = {**arguments, "idempotency_key": self.implementation.name + ":" + self.req["runId"]}
        self._plans[local_id] = (tool, arguments)
        system = governed[tool]["prepareCapability"].split(".", 1)[0]
        names = {definition["name"] for definition in self.list_tools("owner")}
        status_name, login_name = system + "_session_status", system + "_session_login"
        if status_name in names and login_name in names:
            result = await self.client("owner").call_tool(
                status_name, {}, meta=self.host_context(), recovery_class="read"
            )
            observed = result.payload()
            if result.is_error:
                raise RuntimeError("Cannot confirm the original user's session")
            if (
                observed.get("status") in {"expired", "not_found", "login_required"}
                or observed.get("state") == "expired"
            ):
                lease = await self.renew_coordinator_lease(local_id)
                task = {
                    **self.req["task"],
                    "coordinatorLeaseVersion": str(lease["version"]),
                }
                login = await self.client("owner").call_tool(
                    login_name,
                    {},
                    meta={
                        **self.host_context(),
                        "io.agentbridge/task": task,
                        "io.agentbridge/continue-after-login": True,
                    },
                    recovery_class="unsafe",
                )
                await self.process_tool_result(local_id, login)
                return
        await self._execute_plan(local_id, tool, arguments)

    def _identity_profile_meta(self):
        match = re.fullmatch(r"telegram:([0-9]+):([0-9]+)", self.req["endpointKey"])
        if not match:
            return self.host_context()
        return {
            **self.host_context(),
            "io.agentbridge/telegram-identity": {
                "botId": match.group(1),
                "senderId": match.group(2),
            },
        }

    async def check_telegram(self):
        match = re.fullmatch(r"telegram:([0-9]+):([0-9]+)", self.req["endpointKey"])
        if match:
            result = await self.client("owner").call_tool(
                "agentbridge_host_identity_profile",
                {"agent_host": self.implementation.name},
                meta=self._identity_profile_meta(),
                recovery_class="read",
            )
            if (
                result.is_error
                or result.payload().get("telegramIdentityVerified") is not True
            ):
                raise PermissionError("Original Telegram pairing is no longer active")
            if self.implementation.name == "pi":
                current = await self.client("owner").call_tool(
                    "agentbridge_host_telegram_endpoint", {"bot_id": match.group(1), "sender_id": match.group(2)},
                    meta=self.host_context(), recovery_class="read")
                endpoint = current.payload()
                if current.is_error or not re.fullmatch(r"[a-f0-9]{64}", str(self.req.get("pairingRef", ""))) or (
                        endpoint.get("endpointId"), endpoint.get("endpointKey"), endpoint.get("conversationRef"),
                        endpoint.get("workspaceEndpointId"), endpoint.get("pairingRef")) != (
                        self.req["endpoint"], self.req["endpointKey"], self.req["conversation"],
                        self.req.get("workspaceEndpointId"), self.req["pairingRef"]):
                    raise PermissionError("Original Pi private endpoint or pairing generation changed")

    async def present_interaction(self, local_task_id, interaction_id):
        await self.check_telegram()
        result = await self.client("owner").call_tool(
            "agentbridge_host_interaction_present",
            {
                "agent_host": self.implementation.name,
                "endpoint_key": self.req["endpointKey"],
                "interaction_id": interaction_id,
            },
            meta=self.host_context(),
            recovery_class="read",
        )
        payload = result.payload()
        card = payload.get("interaction") or {}
        view = card.get("presentation") or {}
        url = view.get("url", "")
        parsed = urlsplit(url)
        if (
            result.is_error
            or (payload.get("endpoint") or {}).get("endpointId")
            != self.req["endpoint"]
            or card.get("interactionId") != interaction_id
            or card.get("type")
            not in {"business_input", "execution_authorization", "credential"}
            or (self.req.get("commandKind") == QUERY and card.get("type") != "credential")
            or card.get("taskId") != self.req["task"]["taskId"]
            or view.get("owner") != "agentbridge"
            or view.get("modelMustNotCollectValues") is not True
            or parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)
            or "https://" + parsed.netloc not in self.req["trustedCardOrigins"]
        ):
            raise PermissionError("Card does not belong to the bound endpoint")
        with self.journal:
            if self.req.get("commandKind") == QUERY:
                self.journal.execute("INSERT OR IGNORE INTO query_commands VALUES(?,?,?)",
                                     (self.req["runId"], local_task_id, self.req["endpoint"]))
            self.journal.execute(
                """INSERT INTO card_versions VALUES(?,?,1) ON CONFLICT(run) DO UPDATE SET
                   revision=card_versions.revision+1, interaction=excluded.interaction
                   WHERE card_versions.interaction<>excluded.interaction""",
                (self.req["runId"], interaction_id),
            )
            self.journal.execute(
                "UPDATE coordinated SET interaction=?,kind=?,phase='waiting' WHERE run=?",
                (interaction_id, card["type"], self.req["runId"]),
            )
        return card

    async def resume_interaction(self, local_task_id, interaction_id):
        await self.check_telegram()
        await check_skill(self)
        if self.req.get("commandKind") == QUERY:
            task, command = await command_snapshot(self, local_task_id)
            if task.get("status") in {"succeeded", "failed", "canceled", "expired"}:
                if await finish_query(self, local_task_id, task, command):
                    return
                raise PermissionError("Completed query has no matching central read receipt")
            recorded = self.journal.execute(
                "SELECT interaction,kind,phase FROM coordinated WHERE run=?", (self.req["runId"],)).fetchone()
            if (task.get("currentInteractionId") != interaction_id
                    or recorded != (interaction_id, "credential", "waiting")):
                raise PermissionError("Query credential is no longer bound to its original task")
        # Claim BEFORE issuing the non-replayable MCP call, including process death during commit.
        with self.journal:
            claimed = self.journal.execute(
                "INSERT OR IGNORE INTO resumes VALUES(?)", (interaction_id,)
            ).rowcount
            if not claimed:
                return
            self.journal.execute("UPDATE coordinated SET phase='resuming' WHERE run=?", (self.req["runId"],))
        try:
            fixed_pi = (self.implementation.name == "pi" and self.req.get("commandKind") == PREPARE
                        and not self.req.get("boundPlanId") and not self.journal.execute(
                            "SELECT 1 FROM batch_scope WHERE run=?", (self.req["runId"],)).fetchone())
            await super().resume_interaction(local_task_id, interaction_id, with_observation=fixed_pi)
        except Exception:
            if self.req.get("commandKind") == QUERY:
                with self.journal:
                    self.journal.execute("UPDATE coordinated SET phase='query_observe' WHERE run=? AND phase='resuming'",
                                         (self.req["runId"],))
                task, command = await command_snapshot(self, local_task_id)
                if await finish_query(self, local_task_id, task, command):
                    return
            raise

    async def close_interaction(self, local_task_id, interaction):
        if self.req.get("commandKind") != QUERY:
            await super().close_interaction(local_task_id, interaction)
            state = str(interaction.get("state") or "").lower()
            phase = {
                "expired": "expired",
                "declined": "canceled",
                "failed": "failed",
                "superseded": "failed",
                "completed": "closed",
            }.get(state)
            if phase:
                with self.journal:
                    self.journal.execute(
                        "UPDATE coordinated SET phase=? WHERE run=? AND phase='waiting'",
                        (phase, self.req["runId"]),
                    )
            return
        task, command = await command_snapshot(self, local_task_id)
        if (task.get("status") not in {"failed", "canceled", "expired", "succeeded"}
                and interaction.get("state") in {"expired", "declined", "failed", "superseded"}
                and task.get("currentInteractionId") == interaction.get("interactionId")):
            await super().close_interaction(local_task_id, interaction)
            task, command = await command_snapshot(self, local_task_id)
        if not await finish_query(self, local_task_id, task, command):
            raise RuntimeError("Query closure has no terminal central receipt")

    async def process_tool_result(self, local_task_id, result, *, resumed_from=None, resume_nonce=None):
        if self.req.get("boundPlanId"):
            from .bound_plans import current_plan_result
            result = await current_plan_result(self, local_task_id, result)
            if result is None:
                return
        if self.req.get("commandKind") == QUERY:
            if await query_result(self, local_task_id, result, resumed_from):
                await super().process_tool_result(local_task_id, result)
            return
        payload = result.payload()
        card = (
            result.private_meta.get("io.agentbridge/interaction")
            or payload.get("interaction")
            or {}
        )
        if (
            resumed_from
            and not result.is_error
            and payload.get("status") == "succeeded"
            and (payload.get("nextAction") or {}).get("type")
            == "retry_original_request"
            and card.get("type") == "credential"
            and card.get("interactionId") == resumed_from
        ):
            original = self._plans.get(local_task_id)
            if original is None:
                raise RuntimeError("Original prepare inputs are unavailable")
            if original[0] != self.req["tool"]:
                raise RuntimeError("Original prepare scope has changed")
            await check_skill(self)
            await self._execute_plan(local_task_id, original[0], dict(original[1]))
            return
        batch = result.payload().get("batch") or {}
        if (
            batch.get("batchId")
            and batch.get("parentTaskId") == self.req["task"]["taskId"]
        ):
            with self.journal:
                self.journal.execute(
                    "INSERT OR IGNORE INTO batch_scope VALUES(?,?)",
                    (self.req["runId"], batch["batchId"]),
                )
        await super().process_tool_result(
            local_task_id, result, resumed_from=resumed_from, resume_nonce=resume_nonce
        )
        if not resumed_from:
            return

        bound_batch = self.journal.execute(
            "SELECT batch FROM batch_scope WHERE run=?", (self.req["runId"],)
        ).fetchone()
        if bound_batch:
            snapshot = await self.refresh_task_snapshot(local_task_id)
            receipt = snapshot.get("batchVerification") or {}
            if (
                receipt.get("taskId") == self.req["task"]["taskId"]
                and receipt.get("batchId") == bound_batch[0]
                and receipt.get("state") == "succeeded"
                and receipt.get("allVerified") is True
            ):
                with self.journal:
                    self.journal.execute(
                        "UPDATE coordinated SET phase='verified' WHERE run=?",
                        (self.req["runId"],),
                    )
            return

        def ids(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k == "operationId" and isinstance(v, str):
                        yield v
                    else:
                        yield from ids(v)
            elif isinstance(value, list):
                for item in value:
                    yield from ids(item)

        for operation_id in set(ids(result.payload())):
            status = await self.client("owner").call_tool(
                "agentbridge_operation_get",
                {"operation_id": operation_id},
                meta=self.host_context(),
                recovery_class="read",
            )
            op = status.payload().get("operation") or {}
            evidence = (op.get("result") or {}).get("verification") or {}
            if (
                not status.is_error
                and op.get("userSubject") == self.req["subject"]
                and op.get("taskId") == self.req["task"]["taskId"]
                and op.get("capability") == self.req["expectedCommit"]
                and op.get("status") == "succeeded"
                and (
                    evidence.get("confirmed") is True or evidence.get("matched") is True
                )
            ):
                with self.journal:
                    self.journal.execute(
                        "UPDATE coordinated SET phase='verified' WHERE run=?",
                        (self.req["runId"],),
                    )


async def run_locked(req, implementation=IMPLEMENTATION):
    root = Path(req["userDir"])
    run_id = req["runId"]
    task = req["task"]
    account_prefix = "workspace:" + req["instance"] + ":"
    telegram = re.fullmatch(r"telegram:([0-9]+):([0-9]+)", req["endpointKey"])
    basis = req.get("workspaceEndpointKey", "") if telegram else req["endpointKey"]
    command = req.get("mode") == "prepare_command"
    if command:
        req["tool"] = "agentbridge_host_task_prepare"
    if (
        not isinstance(req.get("tool"), str)
        or not basis.startswith(account_prefix)
        or (telegram and not req.get("workspaceEndpointId"))
        or not root.is_absolute()
        or req["conversation"] != task["conversationRef"]
        or req["endpoint"] != task["endpointId"]
        or task["hostRunId"] != run_id
    ):
        raise PermissionError("Invalid trusted task binding")
    account = basis[len(account_prefix) :]
    identity = PersonalIdentity(
        "owner",
        req["tokenFile"],
        req["endpointKey"],
        telegram.group(2) if telegram else account,
        req["conversation"],
        req["subject"],
        req["endpoint"],
        telegram.group(1) if telegram else account,
    )
    journal = sqlite3.connect(root / "coordinated-private.sqlite3", timeout=8)
    with journal:
        journal.execute("""CREATE TABLE IF NOT EXISTS coordinated (run TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
            central_task TEXT NOT NULL, local_task TEXT, interaction TEXT, kind TEXT, phase TEXT NOT NULL)""")
        journal.execute("""CREATE TABLE IF NOT EXISTS query_commands(
            run TEXT PRIMARY KEY, local_task TEXT NOT NULL, endpoint TEXT NOT NULL)""")
        journal.execute("""CREATE TABLE IF NOT EXISTS write_scope (run TEXT PRIMARY KEY, prepare TEXT NOT NULL,
            expected_commit TEXT NOT NULL, argument_digest TEXT NOT NULL)""")
        journal.execute(
            "CREATE TABLE IF NOT EXISTS resumes(interaction TEXT PRIMARY KEY)"
        )
    with journal:
        journal.execute(
            "CREATE TABLE IF NOT EXISTS card_versions(run TEXT PRIMARY KEY,interaction TEXT NOT NULL,revision INTEGER NOT NULL)"
        )
        journal.execute(
            "CREATE TABLE IF NOT EXISTS batch_scope(run TEXT PRIMARY KEY,batch TEXT NOT NULL)"
        )
    with journal:
        journal.execute("CREATE TABLE IF NOT EXISTS write_plan_scope(run TEXT PRIMARY KEY, plan TEXT NOT NULL)")
        prior_plan = journal.execute("SELECT plan FROM write_plan_scope WHERE run=?", (run_id,)).fetchone()
        if prior_plan:
            req["boundPlanId"] = prior_plan[0]
    state = ReferenceHostState(root / "coordinator-state.sqlite3")
    driver = BoundDriver(
        req,
        journal,
        identities=IdentityRegistry([identity]),
        state=state,
        mcp_url=req["mcpUrl"],
        ca_bundle=req.get("caFile"),
        implementation=implementation,
        instance_id=req["instance"],
        private_transport=True,
        load_tool_catalog=not (implementation.name == "pi" and command),
        poll_interval_seconds=0.35,
        maximum_interaction_wait_seconds=238,
    )
    collector = ReferenceHostRuntimeCollector(driver)
    owns_run = False
    try:
        profile = await driver.ensure_identity("owner")
        if telegram and profile.get("telegramIdentityVerified") is not True:
            raise PermissionError("Original Telegram pairing is no longer active")
        if telegram and implementation.name == "pi":
            await driver.check_telegram()
        governed = profile.get("controlledPrepares") or {}
        if command:
            # A pre-created command reveals its exact governed capability only in the central result.
            req["expectedCommit"] = ""
        else:
            contract = governed.get(req["tool"])
            if not isinstance(contract, dict) or req["tool"] not in {
                t["name"] for t in driver.list_tools("owner")
            }:
                raise PermissionError("No controlled prepare for this user and host")
            req["expectedCommit"] = contract["commitCapability"]
            req["expectedPrepare"] = contract["prepareCapability"]
        req["checkedSkillPin"] = pin_run(
            journal, req, None if command else contract["prepareCapability"]
        )
        await check_skill(driver)
        arguments = req.get("arguments", {})
        if not isinstance(arguments, dict) or any(
            k in arguments
            for k in (
                "idempotency_key",
                "authorization_id",
                "input_submission_id",
                "approved",
                "decision",
            )
        ):
            raise PermissionError("Expected public prepare arguments")
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with journal:
            if not req.get("recoveryOnly"):
                journal.execute(
                    "INSERT OR IGNORE INTO write_scope VALUES(?,?,?,?)",
                    (run_id, req["tool"], req["expectedCommit"], digest),
                )
            scope = journal.execute(
                "SELECT prepare,expected_commit,argument_digest FROM write_scope WHERE run=?",
                (run_id,),
            ).fetchone()
            if (
                not scope
                or (
                    scope[0] != req["tool"]
                    or (not command and scope[1] != req["expectedCommit"])
                )
                or (not req.get("recoveryOnly") and scope[2] != digest)
            ):
                return "unknown"
        with journal:
            if (
                req.get("recoveryOnly")
                and not journal.execute(
                    "SELECT 1 FROM coordinated WHERE run=? AND endpoint=? AND central_task=?",
                    (run_id, req["endpoint"], task["taskId"]),
                ).fetchone()
            ):
                return "unknown"
            started = journal.execute(
                "INSERT OR IGNORE INTO coordinated(run,endpoint,central_task,phase) VALUES(?,?,?,'starting')",
                (run_id, req["endpoint"], task["taskId"]),
            ).rowcount
        if started:
            owns_run = True
            local = state.create_task(
                identity_label="owner",
                tool_name=req["tool"],
                conversation_ref=req["conversation"],
                title="原工作台办理"
                if command
                else driver.tool("owner", req["tool"]).get("title", req["tool"]),
            )
            local_id = local["local_task_id"]
            state.bind_central_task(
                local_task_id=local_id,
                task_id=task["taskId"],
                endpoint_id=req["endpoint"],
                lease_version=int(task["coordinatorLeaseVersion"]),
            )
            with journal:
                journal.execute(
                    "UPDATE coordinated SET local_task=? WHERE run=?",
                    (local_id, run_id),
                )
            collector.start()
            if command:
                central_task, original = await command_snapshot(driver, local_id)
                if original.get("state") != "pending":
                    if req["commandKind"] == QUERY and await finish_query(driver, local_id, central_task, original):
                        return "verified" if central_task["status"] == "succeeded" else central_task["status"]
                    return "unknown"
                lease = await driver.renew_coordinator_lease(local_id)
                result = await driver.call_for_task(
                    local_id,
                    "agentbridge_host_task_prepare",
                    {
                        "task_id": task["taskId"],
                        "command_key": req["commandKey"],
                        "session_key": req["conversation"],
                        "endpoint_key": req["endpointKey"],
                    },
                    lease=lease,
                    recovery_class="unsafe",
                )
                data = result.structured
                if not result.is_error and data.get("status") in {"failed", "canceled", "expired"}:
                    terminal, original = await command_snapshot(driver, local_id)
                    if terminal.get("status") in {"failed", "canceled", "expired"}:
                        state.finish_task(local_id, status="canceled" if terminal["status"] == "expired" else terminal["status"])
                        with journal:
                            journal.execute("UPDATE coordinated SET phase=? WHERE run=?", (terminal["status"], run_id))
                        return terminal["status"]
                    return "unknown"
                if (result.is_error or data.get("commandKind") != req["commandKind"]
                        or data.get("capability") != req["commandCapability"]):
                    return "unknown"
                if req["commandKind"] == PREPARE:
                    contract = next((d for d in governed.values() if d["prepareCapability"] == req["commandCapability"]), None)
                    if not contract:
                        return "unknown"
                    req["expectedCommit"] = contract["commitCapability"]
                    with journal:
                        journal.execute("UPDATE write_scope SET expected_commit=? WHERE run=?", (req["expectedCommit"], run_id))
                await driver.process_tool_result(local_id, result)
            elif req.get("mode") == "durable_plan":
                from .bound_plans import begin_write_plan
                await begin_write_plan(driver, local_id, arguments, governed)
            else:
                await driver.begin_governed(local_id, req["tool"], arguments, governed)
        else:
            row = journal.execute(
                "SELECT local_task,phase FROM coordinated WHERE run=? AND endpoint=? AND central_task=?",
                (run_id, req["endpoint"], task["taskId"]),
            ).fetchone()
            if not row or not row[0]:
                return "unknown"
            local_id = row[0]
            if command:
                central_task, original = await command_snapshot(driver, local_id)
                if req["commandKind"] == QUERY and await finish_query(driver, local_id, central_task, original):
                    return "verified" if central_task["status"] == "succeeded" else central_task["status"]
                if row[1] != "waiting":
                    return "unknown"
                local_interaction = state.get_task(local_id).get("active_interaction_id")
                central_interaction = central_task.get("currentInteractionId")
                if central_interaction != local_interaction:
                    # Continuous flow may advance business_input -> execution_authorization during the
                    # brief subprocess handoff gap; keep monitoring local_interaction only if it is
                    # an unresumed business_input bound to this task with resume allowed.
                    if not local_interaction or journal.execute(
                        "SELECT 1 FROM resumes WHERE interaction=?", (local_interaction,)
                    ).fetchone():
                        return "unknown"
                    local_card = await driver.get_interaction(local_id, local_interaction)
                    if (
                        local_card.get("type") != "business_input"
                        or local_card.get("taskId") != task["taskId"]
                        or (local_card.get("resume") or {}).get("allowed") is not True
                    ):
                        return "unknown"
                if req["commandKind"] == PREPARE and not any(
                    d["prepareCapability"] == req["commandCapability"] and d["commitCapability"] == scope[1]
                    for d in governed.values()
                ):
                    return "unknown"
                req["expectedCommit"] = scope[1]
            elif row[1] != "waiting":
                return row[1] if row[1] == "verified" else "unknown"
            else:
                snapshot = await driver.refresh_task_snapshot(local_id)
                central_task = snapshot.get("task") or {}
                if central_task.get("status") in {"failed", "canceled", "expired"}:
                    terminal_status = central_task["status"]
                    state.finish_task(
                        local_id,
                        status="canceled" if terminal_status == "expired" else terminal_status,
                    )
                    with journal:
                        journal.execute(
                            "UPDATE coordinated SET phase=? WHERE run=? AND phase='waiting'",
                            (terminal_status, run_id),
                        )
                    return terminal_status
            owns_run = True
            item = state.get_task(local_id)
            interaction = item.get("active_interaction_id")
            if (
                not interaction
                or journal.execute(
                    "SELECT 1 FROM resumes WHERE interaction=?", (interaction,)
                ).fetchone()
            ):
                return "unknown"
            await driver.renew_coordinator_lease(local_id)
            driver.interactions.start(local_id, {"interactionId": interaction})
        async with asyncio.timeout(237):
            while True:
                row = journal.execute(
                    "SELECT phase FROM coordinated WHERE run=?", (run_id,)
                ).fetchone()
                if row and row[0] in {"verified", "failed", "canceled", "expired"}:
                    return row[0]
                if row and row[0] == "query_observe":
                    return "unknown"
                state_value = state.get_task(local_id)["status"]
                if state_value in {"failed", "canceled", "unknown", "observe_only"}:
                    return "closed" if state_value == "canceled" else "unknown"
                if state_value == "succeeded" and all(
                    t.done() for t in driver.interactions._monitors.values()
                ):
                    return "unknown"
                await asyncio.sleep(0.25)
    except TimeoutError:
        row = journal.execute("SELECT phase FROM coordinated WHERE run=?", (run_id,)).fetchone()
        if owns_run and row and row[0] == "waiting":
            return "waiting"
        return "unknown"
    finally:
        if owns_run:
            await collector.wait_initial(timeout=2.0)
        await collector.stop()
        await driver.close()
        if owns_run:
            with journal:
                if req.get("commandKind") == QUERY:
                    journal.execute("UPDATE coordinated SET phase='query_observe' WHERE run=? AND phase='resuming'", (run_id,))
                journal.execute(
                    "UPDATE coordinated SET phase='unknown' WHERE run=? AND phase NOT IN ('waiting','verified','closed','failed','canceled','expired','query_observe')",
                    (run_id,),
                )
        journal.close()


async def run(req, implementation=IMPLEMENTATION):
    key = hashlib.sha256(req["runId"].encode()).hexdigest()
    path = Path(req["userDir"]) / ("coordinator-" + key + ".lock")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        return await run_locked(req, implementation)


def main():
    # The service chooses its implementation in the deployment command, never from model/pipe JSON.
    if sys.argv[1:] == ["--host-type", "pi"]:
        implementation = PI_IMPLEMENTATION
    elif sys.argv[1:] in ([], ["--host-type", "nanobot"]):
        implementation = IMPLEMENTATION
    else:
        raise SystemExit("Expected --host-type pi or nanobot")
    os.umask(0o077)
    try:
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            raise ValueError
        outcome = asyncio.run(run(json.loads(data), implementation))
    except Exception:
        outcome = "unknown"
    print(json.dumps({"outcome": outcome}), flush=True)


if __name__ == "__main__":
    main()
