"""Authenticated loopback Workspace gateway for Pi. The service owns all active runs."""

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from . import SDK_VERSION, VERSION
from .cards import Cards
from .central import connect, ControlSessions, PLAN
from .coordinator import Coordinator
from .ledger import Conflict, Ledger, UnconfirmedOperation
from .runtime import PiRuntime, private_dir, private_file


BINDING_FIELDS = ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")
PAIRED_DELIVERY_ACTIONS = {"telegram-notifications", "telegram-notification-receipt", "telegram-chat-validate",
    "telegram-chat-receipt", "telegram-artifact-file", "telegram-watch-validate", "telegram-workspace-media",
    "telegram-workspace-result-view", "telegram-workspace-media-v2", "telegram-media-source", "telegram-media-append"}
DELIVERY_ACTIONS = PAIRED_DELIVERY_ACTIONS | {"telegram-task", "telegram-timeline", "artifact-reissue"}


def text_field(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Missing or invalid field")
    return value


def binding_key(record):
    return tuple(text_field(record[field], 1024) for field in BINDING_FIELDS)


def bearer(filename):
    token = private_file(filename).strip()
    if len(token) < 32 or not token.isascii() or any(ch.isspace() for ch in token):
        raise ValueError("Invalid service credential")
    return token


class Host:
    def __init__(self, spec):
        self.spec = spec
        self.instance = text_field(spec["instanceId"], 160)
        self.service_token = spec["serviceTokenFile"]
        service_secret = bearer(self.service_token)
        endpoint = urlsplit(spec["mcpUrl"])
        if (not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or not (endpoint.scheme == "https" or endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1")):
            raise ValueError("Expected secure central MCP or isolated loopback")
        self.bindings = {}
        tokens = {}
        for record in spec["bindings"]:
            key = binding_key(record)
            if key in self.bindings or not key[2].startswith("workspace:" + self.instance + ":"):
                raise ValueError("Unexpected or duplicate Pi Workspace binding")
            personal = bearer(record["tokenFile"])
            if personal == service_secret or (personal in tokens and tokens[personal] != key[0]):
                raise ValueError("Pi credentials must be isolated by subject and purpose")
            tokens[personal] = key[0]
            self.bindings[key] = record
        from .telegram_routes import PiTelegramRoutes
        self.control_sessions = ControlSessions(spec, bearer)
        self.telegram = PiTelegramRoutes(self)
        state = private_dir(spec["stateDir"])
        self.state = state
        if spec.get("requiredLevel", "L1") not in {"L1", "L2", "L3"}:
            raise ValueError("Invalid configured Pi host level")
        self.coordinator = Coordinator(self) if spec.get("requiredLevel") == "L3" and spec.get("coordinatorProgram") else None
        self.commands = None
        if self.coordinator:
            from nanobot_agentbridge.commands import CommandQueue
            self.commands = CommandQueue(self)
        self.runtime = PiRuntime(spec, state)
        self.ledger = Ledger(state, self.instance)
        self.runs = self.ledger
        self.cards = Cards(self.ledger.db, spec.get("trustedCardOrigins", []))
        self.level = spec.get("requiredLevel", "L1")
        self.verified_bindings = set()
        self.health_lock = asyncio.Lock()
        self.checked_at = None
        self.model_invocation = "unverified"
        self.decode_slots = asyncio.Semaphore(2)

    def user_directory(self, record):
        name = hashlib.sha256(record["user_subject"].encode("utf-8")).hexdigest()
        return private_dir(private_dir(self.state / "users") / name)

    def worker_payload(self, record, run_id):
        value = {"hostType": "pi", "subject": record["user_subject"], "endpoint": record["endpoint_id"], "endpointKey": record["endpoint_key"],
                 "conversation": record["session_key"], "instance": self.instance, "userDir": str(self.user_directory(record)),
                 "tokenFile": record["tokenFile"], "mcpUrl": self.spec["mcpUrl"], "caFile": self.spec.get("caFile"),
                 "trustedCardOrigins": self.spec.get("trustedCardOrigins", []), "runId": run_id}
        if self.coordinator:
            value["coordinatorProgram"] = str(self.coordinator.program)
        if self.telegram.bot:
            value["telegramBotId"] = self.telegram.bot
        if record["endpoint_key"].startswith("telegram:"):
            import re
            ref = record["credential_ref"]
            if not re.fullmatch(r"tg-[a-f0-9]{64}", ref):
                raise PermissionError("Original Telegram pairing generation required")
            value["pairingRef"] = ref[3:]
        for field in ("workspaceEndpointId", "workspaceEndpointKey"):
            if field in record:
                value[field] = record[field]
        return value

    @staticmethod
    def worker_environment(directory):
        return {"HOME": str(directory), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}

    @asynccontextmanager
    async def private_session(self, request):
        web = next((r for r in self.bindings.values() if r["user_subject"] == request["subject"]
                    and r["tokenFile"] == request["tokenFile"]), None)
        if not web:
            raise HTTPException(403)
        async with self.bound_session(web) as central:
            record = await self.telegram.current(central.session, central.meta) if request["endpointKey"].startswith("telegram:") else web
            if not record or (record["endpoint_key"], record["endpoint_id"], record["session_key"]) != (
                    request["endpointKey"], request["endpoint"], request["conversation"]):
                raise HTTPException(403)
            if request["endpointKey"].startswith("telegram:") and record["credential_ref"] != "tg-" + str(request.get("pairingRef")):
                raise HTTPException(403)
            request["controlledPrepares"] = dict(central.governed)
            yield central.session, central.meta, central.level

    @asynccontextmanager
    async def bound_session(self, record, *, catalog=True):
        web = self.telegram.web if record["endpoint_key"].startswith("telegram:") else record
        key = binding_key(web)
        async with AsyncExitStack() as stack:
            try:
                central = await stack.enter_async_context(connect(
                    self.spec, record, bearer(record["tokenFile"]), catalog=catalog))
                if central.level != self.level:
                    raise PermissionError("Central has not approved the required Pi level")
            except Exception:
                self.verified_bindings.discard(key)
                raise
            self.verified_bindings.add(key)
            yield central

    async def refresh_bindings(self):
        async def probe(record):
            async with self.bound_session(record, catalog=False):
                pass

        async with self.health_lock:
            now = asyncio.get_running_loop().time()
            if self.checked_at is not None and now - self.checked_at < 10:
                return
            await asyncio.gather(*(probe(record) for record in self.bindings.values()), return_exceptions=True)
            self.checked_at = asyncio.get_running_loop().time()

    async def check(self):
        await self.runtime.check()
        await self.refresh_bindings()
        if self.commands and self.level == "L3":
            for key in self.verified_bindings:
                self.commands.recover_queries(self.bindings[key])

    async def execute(self, record, run, history, emit, message, images, *, trial=None, request_key=None):
        if record["endpoint_key"].startswith("telegram:") and not await self.telegram.verified(binding_key(record)):
            raise PermissionError("Telegram pairing is no longer active")
        async with self.bound_session(record) as central:
            if record["endpoint_key"].startswith("telegram:"):
                async def original_pair():
                    fresh = await self.telegram.current(central.session, central.meta)
                    return fresh is not None and binding_key(fresh) == binding_key(record)
                central.binding_check = original_pair
            if trial is not None:
                if central.level != "L3":
                    raise PermissionError("Private draft trial not available")
                from nanobot_agentbridge.skill_scope import run_trial
                async def complete(prompt):
                    result = await self.invoke_model("completion", run=run, subject=record["user_subject"],
                        system="按固定修订的业务助手说明执行隔离样例。", message=prompt)
                    return result["text"]
                request = {**self.worker_payload(record, run), "trial": trial, "message": message, "requestKey": request_key}
                answer = await run_trial(central.session, request, central.meta, complete)
                emit("delta", answer)
                return answer
            if central.level in {"L2", "L3"}:
                central.handle_card = partial(self.cards.resume_credential, central, record, run)
            if central.level == "L3" and self.coordinator:
                from nanobot_agentbridge.task_context import TaskContext
                from nanobot_agentbridge.telegram_identity import verify_telegram_binding
                from nanobot_agentbridge.skill_scope import SkillRun
                central.task_context = TaskContext(central.session, self.worker_payload(record, run), central.meta,
                    host_type="pi", verify_telegram=partial(verify_telegram_binding, host_type="pi"))
                central.skill = SkillRun(central.session, central.task_context.get)
                central.handle_prepare = lambda tool, args, pin: self.coordinator.dispatch(central, record, run, tool, args, skill_pin=pin)
                request = self.worker_payload(record, run)
                from nanobot_agentbridge.watch_actions import WatchActions, WATCH_MUTATIONS
                for name in WATCH_MUTATIONS.intersection(central.definitions):
                    definition = central.definitions[name]
                    modeled = SimpleNamespace(name=name, description=definition["description"], inputSchema=definition["parameters"])
                    action = WatchActions(modeled, request, central.task_context, central.skill, error=str, host_type="pi")
                    definition.update(parameters=action.parameters, description=action.description)
                    central.private_tools[name] = action
                if "agentbridge_skill_authoring" in central.definitions:
                    from nanobot_agentbridge.draft_actions import AUTHORING, DraftActions
                    async def sample(system, prompt):
                        return await self.invoke_model("completion", subject=record["user_subject"], system=system, message=prompt)
                    action = DraftActions(request, central.task_context, central.skill, complete=sample, error=str, host_type="pi")
                    central.private_tools[AUTHORING] = action
                    central.definitions[AUTHORING].update(parameters=action.parameters, description=action.description)
                if central.file_tools:
                    from nanobot_agentbridge.file_generation import DESCRIPTIONS, TaskFiles, parameters
                    central.files = TaskFiles()
                    central.file_binding = SimpleNamespace(endpoint=record["endpoint_id"], conversation=record["session_key"])
                    for name in central.file_tools.intersection(central.definitions):
                        definition = central.definitions[name]
                        definition.update(parameters=parameters(name, definition["parameters"]),
                                          description=DESCRIPTIONS.get(name, definition["description"]))
                if PLAN in central.definitions:
                    from nanobot_agentbridge.plan_submission import PlanSubmission
                    from .planning import PlanningRepair
                    async def govern(_request, _context, tool, arguments, pin, *, plan):
                        outcome = await self.coordinator.dispatch(central, record, run, tool, arguments, plan=plan, skill_pin=pin)
                        return {"verified": "中央已核验原计划的办理结果。", "waiting": "原计划等待可信卡确认。", "closed": "原计划可信卡已关闭。"}.get(outcome, "原计划办理结果待核对。")
                    central.plan = PlanSubmission(self.worker_payload(record, run), central.task_context,
                        SimpleNamespace(inputSchema=central.definitions[PLAN]["parameters"]), central.skill,
                        central.contextual_reads.intersection(central.definitions), central.governed, host_type="pi", govern=govern)
                    central.repair = PlanningRepair(central)
            event = await self.invoke_model("chat", run=run, subject=record["user_subject"], history=history,
                                            emit=emit, central=central, message=message, images=images)
            if central.task_context:
                await central.task_context.finish_confirmed_read()
            self.ledger.record_usage(run, event.get("usage"))
            if central.governed_result:
                outcome, tool = central.governed_result
                if outcome == "verified":
                    answer = "中央已核验请假草稿保存成功，流程尚未提交。" if tool == "oa_leave_prepare" else "中央已核验原任务的业务操作成功。"
                elif outcome in {"closed", "expired", "canceled", "failed"}:
                    answer = "原任务的可信办理已结束，未核验成功写入。"
                elif outcome == "waiting":
                    answer = "原任务正在等待你的可信卡确认。"
                else:
                    raise UnconfirmedOperation()
                emit("delta", answer)
                return answer
            if central.repair and central.repair.source:
                answer = central.repair.final_answer(event["text"])
                emit("delta", answer)
                return answer
            return event["text"]

    async def invoke_model(self, *args, **kwargs):
        try:
            event = await self.runtime.invoke(*args, **kwargs)
        except Exception:
            self.model_invocation = "last_unconfirmed"
            raise
        self.model_invocation = "last_succeeded"
        return event

    async def action(self, action, body):
        if action == 'telegram-media-append':
            from aiohttp import web
            from nanobot_agentbridge.telegram_media_receipts import recover_route
            try:
                previous = await recover_route(self, body, host_type='pi')
            except web.HTTPException as exc:
                raise HTTPException(exc.status) from None
            if previous is not None:
                return previous
        if action == 'telegram-workspace-media' and body.get('action') == 'ack' and 'pairingRef' in body:
            from .media_receipts import acknowledge
            return await acknowledge(self, body)
        if action in {"telegram-workspace-result-view", "telegram-workspace-media-v2"} and body.get("action") == "ack" and "pairingRef" in body:
            from aiohttp import web
            from nanobot_agentbridge.workspace_query_delivery import acknowledge_receipt
            try:
                return await acknowledge_receipt(self, body, host_type="pi")
            except web.HTTPException as exc:
                raise HTTPException(exc.status) from None
        if action == "ready":
            await self.refresh_bindings()
            verified, configured = len(self.verified_bindings), len(self.bindings)
            available = self.runtime.ready and verified > 0
            caps = ["chat", "skill_model"]
            if self.level in {"L2", "L3"}:
                caps.append("credential_card")
            if self.level == "L3" and self.coordinator:
                caps.extend(("skills", "skill_trial"))
            if self.runtime.images:
                caps.append("attachments")
            return {"available": available, "sdkVersion": SDK_VERSION,
                    "modelConfiguration": "configured", "modelInvocation": self.model_invocation,
                    "configuredBindings": configured, "verifiedBindings": verified,
                    "centralConnection": "unavailable" if not verified else "verified" if verified == configured else "partial",
                    "acceptedLevel": self.level if verified else None, "capabilities": caps if available else []}
        if action in {"telegram-route", "telegram-present", "telegram-enroll", "telegram-mirror-peers"}:
            return await self.telegram.action(action, body)
        key = binding_key(body["binding"])
        if action in PAIRED_DELIVERY_ACTIONS:
            record = await self.telegram.delivery_hint(key)
        else:
            record = self.bindings.get(key) or await self.telegram.verified(key)
        if action == "telegram-binding":
            return {"verified": bool(record and key[2].startswith("telegram:"))}
        if not record:
            raise HTTPException(403)
        if action == "prepare-command":
            if not self.commands or self.level != "L3":
                raise HTTPException(403)
            from aiohttp import web
            try:
                # The authenticated route selected this exact local record. A fixed
                # command only needs current authority, its task snapshot and lease.
                async with self.control_sessions.borrow(record, lane="prepare-command") as central:
                    if central.level != "L3":
                        raise HTTPException(403)
                    if record["endpoint_key"].startswith("telegram:"):
                        current = await self.telegram.current(central.session, central.meta)
                        if current != record:
                            raise HTTPException(403)
                    return await self.commands.accept(record, body,
                        verified_session=(central.session, central.meta, central.level))
            except web.HTTPException as exc:
                raise HTTPException(exc.status) from None
        if action in DELIVERY_ACTIONS:
            if action == "telegram-task" and self.level == "L2" and key[2].startswith("telegram:"):
                target = body.get("target") or {}
                if body.get("operation") != "continue" or target.get("kind") != "run":
                    return {"state": "unavailable"}
                current = await self.action("card", {"requestKey": target.get("requestKey"), "runId": target.get("id"), "binding": record})
                return {"interaction": current["interaction"]} if current.get("available") else {"state": "unavailable"}
            if self.level != "L3" or not self.coordinator:
                raise HTTPException(403)
            from .channel_delivery import dispatch
            return await dispatch(self, record, action, body)
        if action == "skill-model":
            if key[2].startswith("telegram:"):
                raise HTTPException(403)
            system = text_field(body.get("system"), 56000)
            prompt = text_field(body.get("prompt"), 24000)
            # Re-check the exact principal even though a sample receives no business tools.
            async with self.bound_session(record, catalog=False):
                result = await self.invoke_model("completion", subject=key[0], message=prompt, system=system)
            return {k: result[k] for k in ("execution", "text", "model", "usage")}
        request_key = text_field(body.get("requestKey"), 512)
        binding = json.dumps(key, ensure_ascii=False)
        if action == "accept":
            if not self.runtime.ready:
                raise HTTPException(503)
            try:
                async with self.bound_session(record, catalog=False):
                    pass
            except Exception:
                raise HTTPException(403) from None
            message = text_field(body.get("message"), 100_000)
            trial = body.get("trial")
            if trial is not None:
                from nanobot_agentbridge.skill_scope import valid_trial
                if self.level != "L3" or not key[2].startswith("workspace:") or not valid_trial(trial, message) or body.get("attachments"):
                    raise HTTPException(400)
            if message.lstrip().startswith("/"):
                raise HTTPException(400)
            items = body.get("attachments", [])
            if items == []:
                images, ids, documents = [], [], ""
            else:
                from .media import decode_inputs
                async with self.decode_slots:
                    images, ids, documents = await asyncio.to_thread(decode_inputs, items, telegram=key[2].startswith("telegram:"))
            if images and not self.runtime.images:
                raise HTTPException(400)
            digest_parts = [message, ids] if trial is None else [message, ids, trial]
            digest = hashlib.sha256(json.dumps(digest_parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            row = self.ledger.accept(key[0], binding, request_key, digest, message,
                    lambda run, history, emit: self.execute(record, run, history, emit, message + ("\n\n" + documents if documents else ""), images,
                                                          trial=trial, request_key=request_key))
            return {"runId": row["id"], "state": row["state"]}
        row = self.ledger.lookup(binding, request_key)
        waiting = self.cards.waiting(row["id"]) if row and row["state"] == "accepted" else None
        governed = self.coordinator.waiting(record, row["id"]) if row and self.coordinator else None
        waiting = governed or waiting
        pending = None
        if (row and row["state"] in {"accepted", "completed"} and action in {"lookup", "events", "result-view"}
                and self.level == "L3" and self.coordinator and key[2].startswith("telegram:")):
            from nanobot_agentbridge.task_context import pending_query_for_run
            pending = pending_query_for_run(self.user_directory(record), row["id"], key[1])
        flags = {"hasPrivateCard": bool(waiting), "cardRevision": waiting["revision"] if waiting else 0,
                 "hasResultView": bool(pending)}
        if action == "lookup":
            if not row:
                return {"runId": None}
            return {"runId": row["id"], "state": row["state"], "answer": row["answer"], **flags}
        if row is None or row["id"] != body.get("runId"):
            raise HTTPException(403)
        if action == "cancel":
            governed_run = self.coordinator and row["id"] in self.coordinator.governed_runs
            await self.ledger.stop(row["id"], "unknown" if governed_run else "stopped")
            return {"runId": row["id"], "state": self.ledger.lookup(binding, request_key)["state"]}
        if action == "events":
            after = body.get("after", 0)
            if type(after) is not int or not 0 <= after <= 2**63 - 1:
                raise HTTPException(400)
            return {"events": self.ledger.events(row["id"], after), "answer": row["answer"], **flags}
        if action == "card" and waiting:
            async with self.bound_session(record, catalog=False) as central:
                if central.level in {"L2", "L3"}:
                    card = await self.cards.present(central, record, waiting["interaction"],
                        task=waiting["task"], kind=governed["kind"] if governed else "credential")
                    if card:
                        return {**card, "cardRevision": waiting["revision"]}
        if action == "result-view" and pending:
            from nanobot_agentbridge.telegram_presentation import normalize_result_view
            async with self.private_session(self.worker_payload(record, row["id"])) as (mcp, meta, level):
                if level != "L3":
                    return {"available": False}
                issued = await mcp.call_tool("agentbridge_result_view", {"operation":"issue","task_id":pending["taskId"]},
                    meta={**meta, "io.agentbridge/task":{"taskId":pending["taskId"],"coordinatorLeaseVersion":str(pending["version"]),
                          "endpointId":key[1], "conversationRef":key[3],"hostRunId":row["id"]}})
                view = normalize_result_view((issued.structuredContent or {}).get("resultView"), source_task_id=pending["taskId"],
                        endpoint_id=key[1], allowed_origins=self.cards.origins) if not issued.isError else None
            if view:
                return {"available":True, "resultView":view, "count":pending["count"], "complete":pending["complete"]}
        if action in {"card", "result-view"}:
            return {"available": False}
        raise HTTPException(404)

    async def handle(self, request: Request):
        try:
            header = request.headers.get("Authorization", "")
            if not header.isascii() or not hmac.compare_digest(header, "Bearer " + bearer(self.service_token)):
                raise HTTPException(401)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise HTTPException(400)
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > 18 * 1024 * 1024:
                    raise HTTPException(413)
                content.extend(chunk)
            body = json.loads(content)
            if not isinstance(body, dict) or body.get("instance") != self.instance or body.get("hostType", "pi") != "pi":
                raise HTTPException(403)
            result = await self.action(request.path_params["action"], body)
            return JSONResponse({**result, "instance": self.instance, "hostType": "pi", "adapterVersion": VERSION},
                                headers={"Cache-Control": "no-store"})
        except Conflict:
            raise HTTPException(409) from None
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise HTTPException(400) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503) from None


def create_app(spec):
    host = Host(spec)

    @asynccontextmanager
    async def lifespan(_):
        recovery = None
        try:
            await host.check()
            if host.level == "L3" and host.coordinator:
                recovery = asyncio.create_task(host.coordinator.recover())
            yield
        finally:
            if recovery is not None:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            if host.commands:
                await host.commands.close()
            await host.control_sessions.close()
            await host.ledger.close()

    app = Starlette(routes=[Route("/v1/{action}", host.handle, methods=["POST"])], lifespan=lifespan)
    app.state.host = host
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    config = parser.parse_args().config
    os.umask(0o077)
    spec = json.loads(private_file(config))
    uvicorn.run(create_app(spec), host="127.0.0.1", port=int(spec["listenPort"]), access_log=False, ws="none")


if __name__ == "__main__":
    main()
