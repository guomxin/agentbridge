"""Trusted pipe entry for one prebound NanoBot Workspace task. No listener or model access."""

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
from .host_profile import CoordinatorImplementation
from .identity import IdentityConfig, IdentityRegistry
from .runtime_collector import ReferenceHostRuntimeCollector
from .state import ReferenceHostState
from .task_driver import ReferenceTaskDriver

IMPLEMENTATION = CoordinatorImplementation(
    "nanobot", "0.3.0", ("web_private", "telegram_private")
)


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
            arguments = {**arguments, "idempotency_key": "nanobot:" + self.req["runId"]}
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

    async def check_telegram(self):
        match = re.fullmatch(r"telegram:([0-9]+):([0-9]+)", self.req["endpointKey"])
        if match:
            result = await self.client("owner").call_tool(
                "agentbridge_host_identity_profile",
                {"agent_host": "nanobot"},
                meta={
                    **self.host_context(),
                    "io.agentbridge/telegram-identity": {
                        "botId": match.group(1),
                        "senderId": match.group(2),
                    },
                },
                recovery_class="read",
            )
            if (
                result.is_error
                or result.payload().get("telegramIdentityVerified") is not True
            ):
                raise PermissionError("Original Telegram pairing is no longer active")

    async def present_interaction(self, local_task_id, interaction_id):
        await self.check_telegram()
        result = await self.client("owner").call_tool(
            "agentbridge_host_interaction_present",
            {
                "agent_host": "nanobot",
                "endpoint_key": self.req.get(
                    "workspaceEndpointKey", self.req["endpointKey"]
                ),
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
            != self.req.get("workspaceEndpointId", self.req["endpoint"])
            or card.get("interactionId") != interaction_id
            or card.get("type")
            not in {"business_input", "execution_authorization", "credential"}
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
        # Claim BEFORE issuing the non-replayable MCP call, including process death during commit.
        with self.journal:
            claimed = self.journal.execute(
                "INSERT OR IGNORE INTO resumes VALUES(?)", (interaction_id,)
            ).rowcount
            self.journal.execute(
                "UPDATE coordinated SET phase='resuming' WHERE run=?",
                (self.req["runId"],),
            )
        if not claimed:
            return
        await super().resume_interaction(local_task_id, interaction_id)

    async def process_tool_result(self, local_task_id, result, *, resumed_from=None):
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
            local_task_id, result, resumed_from=resumed_from
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


async def run_locked(req):
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
    state = ReferenceHostState(root / "coordinator-state.sqlite3")
    driver = BoundDriver(
        req,
        journal,
        identities=IdentityRegistry([identity]),
        state=state,
        mcp_url=req["mcpUrl"],
        ca_bundle=req.get("caFile"),
        implementation=IMPLEMENTATION,
        instance_id=req["instance"],
        private_transport=True,
        poll_interval_seconds=0.35,
        maximum_interaction_wait_seconds=238,
    )
    collector = ReferenceHostRuntimeCollector(driver)
    owns_run = False
    try:
        profile = await driver.ensure_identity("owner")
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
            await collector.collect_once()
            collector.start()
            if command:
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
                data = result.payload()
                contract = next(
                    (
                        d
                        for d in governed.values()
                        if d["prepareCapability"] == data.get("capability")
                    ),
                    None,
                )
                if result.is_error or not contract:
                    return "unknown"
                req["expectedCommit"] = contract["commitCapability"]
                with journal:
                    journal.execute(
                        "UPDATE write_scope SET expected_commit=? WHERE run=?",
                        (req["expectedCommit"], run_id),
                    )
                await driver.process_tool_result(local_id, result)
            else:
                await driver.begin_governed(local_id, req["tool"], arguments, governed)
        else:
            row = journal.execute(
                "SELECT local_task,phase FROM coordinated WHERE run=? AND endpoint=? AND central_task=?",
                (run_id, req["endpoint"], task["taskId"]),
            ).fetchone()
            if not row or row[1] != "waiting" or not row[0]:
                return row[1] if row and row[1] == "verified" else "unknown"
            local_id = row[0]
            owns_run = True
            if command:
                req["expectedCommit"] = scope[1]
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
                if row and row[0] == "verified":
                    return "verified"
                state_value = state.get_task(local_id)["status"]
                if state_value in {"failed", "canceled", "unknown", "observe_only"}:
                    return "closed" if state_value == "canceled" else "unknown"
                if state_value == "succeeded" and all(
                    t.done() for t in driver.interactions._monitors.values()
                ):
                    return "unknown"
                await asyncio.sleep(0.25)
    finally:
        await collector.stop()
        await driver.close()
        if owns_run:
            with journal:
                journal.execute(
                    "UPDATE coordinated SET phase='unknown' WHERE run=? AND phase NOT IN ('waiting','verified','closed')",
                    (run_id,),
                )
        journal.close()


async def run(req):
    key = hashlib.sha256(req["runId"].encode()).hexdigest()
    path = Path(req["userDir"]) / ("coordinator-" + key + ".lock")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        return await run_locked(req)


def main():
    os.umask(0o077)
    try:
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            raise ValueError
        outcome = asyncio.run(run(json.loads(data)))
    except Exception:
        outcome = "unknown"
    print(json.dumps({"outcome": outcome}), flush=True)


if __name__ == "__main__":
    main()
