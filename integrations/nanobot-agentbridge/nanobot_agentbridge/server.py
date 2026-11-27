"""Private loopback HTTP transport for a NanoBot Workspace instance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit
from .skill_scope import valid_trial

import httpx
from aiohttp import web

from .private_files import private_directory, private_file, private_text
from .cards import CredentialCards, coordinated_waiting, present_coordinated, present_credential
from .media import clear_stale_images, staged_attachments, validate_attachments
from .runs import RunCoordinator
from .session_pool import McpSessionPool, open_host_session
from .worker import channel_for


BINDING_KEYS = (
    "user_subject",
    "endpoint_id",
    "endpoint_key",
    "session_key",
    "credential_ref",
)


def binding_key(value: dict) -> tuple[str, ...]:
    result = tuple(value[key] for key in BINDING_KEYS)
    if any(
        not isinstance(field, str) or not field.strip() or len(field) > 1024
        for field in result
    ):
        raise ValueError("Invalid host binding")
    return result


def checked_mcp_url(url: str) -> str:
    value = urlsplit(url)
    if (
        not value.hostname
        or value.username
        or value.password
        or value.fragment
        or value.query
        or not (
            value.scheme == "https"
            or (value.scheme == "http" and value.hostname == "127.0.0.1")
        )
    ):
        raise ValueError("Expected a secure MCP URL or a loopback listener")
    return url


def identity_denial(exc: Exception) -> bool:
    if isinstance(exc, ExceptionGroup):
        return all(identity_denial(child) for child in exc.exceptions)
    return isinstance(exc, PermissionError) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}
    )


class Host:
    def __init__(self, spec: dict) -> None:
        self.instance = spec["instanceId"]
        if not isinstance(self.instance, str) or not self.instance.strip():
            raise ValueError("Missing instance ID")
        self.service_token = Path(spec["serviceTokenFile"])
        self.model_oauth = Path(spec["modelOAuthTokenFile"]) if spec.get("modelOAuthTokenFile") else None
        if self.model_oauth is not None:
            # oauth-cli-kit writes its refresh lock next to the shared token.
            private_directory(self.model_oauth.parent)
            with private_file(self.model_oauth):
                pass
            if self.model_oauth.samefile(self.service_token):
                raise ValueError("Model and service credentials must be independent")
        self.auth_token()
        self.mcp = checked_mcp_url(spec["mcpUrl"])
        self.ca_file = spec.get("caFile")
        self.card_origins = spec.get("trustedCardOrigins",[])
        if not isinstance(self.card_origins,list):
            raise ValueError("Expected HTTPS card origins")
        for origin in self.card_origins:
            parsed = urlsplit(origin)
            if (not parsed.hostname or parsed.scheme!="https" or parsed.username or parsed.password or
                    origin != f"https://{parsed.netloc}"):
                raise ValueError("Invalid trusted card origin")
        self.skill_trials = spec.get("enableSkillTrials") is True
        if self.skill_trials:
            from .skills import run_workspace_trial
        self.skills = spec.get("enableSkills") is True
        if self.skills:
            from .skills import SkillRun
            if not spec.get("coordinatorProgram"):
                raise ValueError("Task-bound skills require the coordinator")
        self.coordinator = spec.get("coordinatorProgram")
        if self.coordinator:
            program = Path(self.coordinator)
            st = program.lstat()
            if (not self.card_origins or not program.is_absolute() or not stat.S_ISREG(st.st_mode) or
                st.st_uid not in {os.getuid(),0} or st.st_mode & 0o022 or not os.access(program,os.X_OK)):
                raise ValueError("Invalid private coordinator program")
        self.state = private_directory(Path(spec["stateDir"]))
        self.bindings = {}
        self.subject_locks = {}
        for record in spec["bindings"]:
            key = binding_key(record)
            if key in self.bindings:
                raise ValueError("Duplicate host binding")
            # Confirm permission and availability at startup. Secrets are read again for each run.
            model = json.loads(private_text(Path(record["modelConfigFile"])))
            if self.model_oauth is not None:
                provider = model.get("agents",{}).get("defaults",{}).get("provider","")
                if provider.replace("-","_") != "openai_codex" or any(
                    self.model_oauth.samefile(Path(record[field])) for field in ("modelConfigFile","tokenFile")
                ):
                    raise ValueError("Shared model OAuth requires an independent OpenAI Codex configuration")
            private_text(Path(record["tokenFile"]))
            self.bindings[key] = record
            self.subject_locks.setdefault(key[0], asyncio.Lock())
        from .telegram_routes import TelegramRoutes
        self.telegram_routes = TelegramRoutes(self,spec)
        from .enrollment import Enrollment
        self.enrollment = Enrollment(self, spec.get("enrollmentSenders", {}))
        self.runs = RunCoordinator(self.state / "runs")
        from .commands import CommandQueue
        self.commands=CommandQueue(self)
        self.mcp_pool = McpSessionPool()
        for directory in (self.state / "users").glob("*"):
            if directory.is_dir() and not directory.is_symlink():
                clear_stale_images(directory)

    def auth_token(self) -> str:
        token = private_text(self.service_token).strip()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("Invalid service credential")
        return token

    def worker_payload(self, record, run_id) -> dict:
        subject = record["user_subject"]
        directory = private_directory(
            self.state / "users" / hashlib.sha256(subject.encode()).hexdigest()
        )
        payload = {key: record[key] for key in ("tokenFile", "modelConfigFile")}
        payload.update(
            subject=subject,
            endpoint=record["endpoint_id"],
            conversation=record["session_key"],
            runId=run_id,
            trustedCardOrigins=self.card_origins,
            instance=self.instance,
            mcpUrl=self.mcp,
            userDir=str(directory),
            caFile=self.ca_file,
            coordinatorProgram=self.coordinator,
            enableSkills=self.skills,
            endpointKey=record["endpoint_key"],
            telegramBotId=self.telegram_routes.bot,
        )
        if "workspaceEndpointId" in record and "workspaceEndpointKey" in record:
            payload.update(workspaceEndpointId=record["workspaceEndpointId"], workspaceEndpointKey=record["workspaceEndpointKey"])
        return payload

    def worker_environment(self, directory: Path) -> dict[str,str]:
        env = {
            "HOME": str(directory),
            "TMPDIR": str(private_directory(directory / "tmp")),
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "NO_PROXY": "*",
        }
        if self.model_oauth is not None:
            # Never let the SDK import a local CLI login if this dedicated token becomes unavailable.
            if (directory / ".codex" / "auth.json").exists():
                raise PermissionError("Unexpected CLI credential in isolated worker home")
            with private_file(self.model_oauth):
                pass
            env["OAUTH_CLI_KIT_TOKEN_PATH"] = str(self.model_oauth)
        return env

    async def execute(self, record, run_id, message, emit, *, trial=None, request_key=None, images=()) -> str:
        directory = Path(self.worker_payload(record, run_id)["userDir"])
        with staged_attachments(directory, images) as media:
            return await self._execute_worker(record, run_id, message, emit, trial=trial,
                                              request_key=request_key, media=media)

    async def _execute_worker(self, record, run_id, message, emit, *, trial, request_key, media) -> str:
        payload = self.worker_payload(record,run_id)
        payload.update(message=message,trial=trial,requestKey=request_key,media=media)
        subject = record["user_subject"]
        directory = Path(payload["userDir"])
        # The CLI module belongs to this installation, including in a source checkout.
        bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_module('nanobot_agentbridge.worker',run_name='__main__')"
        env = self.worker_environment(directory)
        async with self.subject_locks[subject]:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                bootstrap,
                str(Path(__file__).resolve().parents[1]),
                env=env,
                cwd=directory,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=1_048_576,
            )
            final = None
            try:
                async with asyncio.timeout(300):
                    process.stdin.write(json.dumps(payload).encode())
                    await process.stdin.drain()
                    process.stdin.close()
                    async for line in process.stdout:
                        event = json.loads(line)
                        text = event.get("text")
                        if (
                            not isinstance(text, str)
                            or event.get("kind") not in {"delta", "final"}
                            or final is not None
                        ):
                            raise ValueError("Invalid worker output")
                        if event["kind"] == "delta":
                            emit(text)
                        else:
                            final = text
                    status = await process.wait()
                if status or final is None:
                    raise RuntimeError("SDK outcome unavailable")
                return final
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def handle(self, request: web.Request) -> web.Response:
        try:
            expected = "Bearer " + self.auth_token()
        except (ValueError, OSError):
            raise web.HTTPServiceUnavailable() from None
        header = request.headers.get("Authorization", "")
        if not header.isascii() or not hmac.compare_digest(header, expected):
            raise web.HTTPUnauthorized()
        if request.content_type != "application/json":
            raise web.HTTPBadRequest()
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("instance") != self.instance:
                raise web.HTTPForbidden()
            action = request.match_info["action"]
            result = await self.action(action, body)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest() from None
        except web.HTTPException:
            raise
        except Exception:
            raise web.HTTPServiceUnavailable() from None
        return web.json_response(
            {"instance": self.instance, **result}, headers={"Cache-Control": "no-store"}
        )

    async def action(self, action: str, body: dict) -> dict:
        if action == "telegram-timeline" and body.get("receiptOnly") is True:
            from .telegram_history_receipts import recover_route
            return await recover_route(self, body, host_type="nanobot")
        if action == "telegram-media-append":
            from .telegram_media_receipts import recover_route
            recovered = await recover_route(self,body,host_type="nanobot")
            if recovered is not None:
                return recovered
        if action in {"telegram-workspace-result-view","telegram-workspace-media-v2"} and body.get("action") == "ack" and "pairingRef" in body:
            from .workspace_query_delivery import acknowledge_receipt
            return await acknowledge_receipt(self, body, host_type="nanobot")
        if action == "ready":
            return {"available":bool(self.bindings or self.enrollment.identities),"adapterVersion":"0.3.1" if self.coordinator else "0.2.0",
                    "capabilities":["chat", "attachments", *(["credential_card"] if self.card_origins else []), *(["skill_trial"] if self.skill_trials else []), *(["skills","skill_model"] if self.skills else [])]}
        if action == "telegram-enroll":
            return {"confirmed":await self.enrollment.confirm(body["botId"],body["senderId"],body["code"])}
        if action == "telegram-mirror-peers":
            return await self.telegram_routes.mirror_peers(body)
        if action in {"telegram-route","telegram-present"}:
            if action == "telegram-route" and body.get("botId") == self.telegram_routes.bot:
                await self.enrollment.web_record(body.get("senderId"))
            return await self.telegram_routes.action(action,body)
        if not isinstance(body.get("binding"), dict):
            raise web.HTTPForbidden()
        key = binding_key(body["binding"])
        record = self.bindings.get(key) or self.telegram_routes.get(key) or await self.enrollment.resolve_web(key)
        if record is None:
            raise web.HTTPForbidden()
        binding = dict(subject=key[0], endpoint=key[1], conversation=key[3])
        if action == "skill-model":
            if not self.skills or channel_for(record["endpoint_key"]) != "workspace":
                raise web.HTTPForbidden()
            from .skill_model import complete_for_binding
            return await complete_for_binding(self, record, body["system"], body["prompt"])
        if action=="prepare-command":
            return await self.commands.accept(record,body)
        if action == "telegram-received-source":
            from .telegram_reception import receive
            return await receive(self,record,body)
        if action in {"telegram-media-source","telegram-media-append"}:
            from .telegram_media_receipts import prepare_source,publish_media
            return await (prepare_source if action=="telegram-media-source" else publish_media)(self,record,body)
        if action == "telegram-timeline":
            from .telegram_timeline import publish_timeline
            return await publish_timeline(self, record, body)
        if action in {"telegram-notifications","telegram-notification-receipt","telegram-chat-validate","telegram-chat-receipt","telegram-artifact-file","telegram-watch-validate","telegram-workspace-result-view","telegram-workspace-media-v2"}:
            from .notifications import notifications
            return await notifications(self,record,action,body)
        if action == "telegram-task":
            from .telegram_tasks import navigate
            return await navigate(self, record, body)
        if action == "telegram-binding":
            if channel_for(record["endpoint_key"])!="telegram":
                raise web.HTTPForbidden()
            from .telegram_identity import verify_telegram_binding
            try:
                async with open_host_session(self, self.worker_payload(record,None), lane="control") as (session,meta,_):
                    verified = await verify_telegram_binding(session,self.instance,key[0],meta,record["endpoint_key"])
                    if verified:
                        from .telegram_reception import scope
                        try:
                            await scope(session,meta,record)
                        except web.HTTPForbidden:
                            verified=False
            except Exception as exc:
                if not identity_denial(exc):
                    raise
                verified = False
            return {"verified":verified}
        request_key = body["requestKey"]
        if (
            not isinstance(request_key, str)
            or not request_key.strip()
            or len(request_key) > 512
        ):
            raise web.HTTPBadRequest()
        if action == "accept":
            message = body["message"]
            channel = channel_for(record["endpoint_key"])
            if channel == "telegram":
                from .telegram_reception import validate
                await validate(self,record,request_key)
            images = validate_attachments(body.get("attachments"), telegram=channel == "telegram")
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message) > 100_000
                or message.lstrip().startswith("/")
                or (images and (body.get("trial") is not None or channel not in {"workspace", "telegram"}))
                or (body.get("trial") is not None and
                    (not self.skill_trials or channel_for(record["endpoint_key"])!="workspace" or not valid_trial(body["trial"],message)))
            ):
                raise web.HTTPBadRequest()
            trial = body.get("trial")
            encoded = json.dumps([message,trial],sort_keys=True,ensure_ascii=False).encode() if trial else message.encode()
            if images:
                encoded = json.dumps([message, [image.identity() for image in images]], ensure_ascii=False).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            try:
                entry = self.runs.accept(
                    **binding,
                    request_key=request_key,
                    payload_digest=digest,
                    public_prompt=message,
                    execute=lambda run_id, emit: self.execute(record, run_id, message, emit,trial=trial,request_key=request_key,images=images),
                )
            except ValueError:
                raise web.HTTPConflict() from None
            return {"runId": entry["run_id"], "state": entry["state"]}
        entry = self.runs.lookup(request_key, **binding)
        waiting = None
        task_card = None
        pending_query = None
        work = None
        if entry and self.card_origins and entry["state"] in {"accepted","unknown"}:
            work = self.worker_payload(record,entry["run_id"])
            if entry["state"]=="accepted":
                waiting = CredentialCards(Path(work["userDir"])).waiting_card(entry["run_id"],key[1])
            if self.coordinator:
                task_card=coordinated_waiting(Path(work["userDir"]),entry["run_id"],key[1])
        if entry and self.card_origins and self.coordinator and entry["state"] in {"accepted", "completed"} and channel_for(record["endpoint_key"]) == "telegram":
            work = work or self.worker_payload(record, entry["run_id"])
            from .task_context import pending_query_for_run
            pending_query = pending_query_for_run(Path(work["userDir"]), entry["run_id"], key[1])
        card_revision = (task_card or waiting or {}).get("revision", 0)
        if action == "lookup":
            if entry is None:
                return {"runId": None}
            return {
                "runId": entry["run_id"],
                "state": entry["state"],
                "answer": entry["answer"],
                "hasPrivateCard": bool(waiting or task_card),
                "cardRevision": card_revision,
                "hasResultView": bool(pending_query),
            }
        if entry is None or body.get("runId") != entry["run_id"]:
            raise web.HTTPForbidden()
        if action == "artifact-reissue":
            from .artifacts import reissue
            return await reissue(self, record, body, entry["run_id"])
        if action == "events":
            after = body.get("after", 0)
            if type(after) is not int or after < 0:
                raise web.HTTPBadRequest()
            return {
                "events": self.runs.events(entry["run_id"], after=after, **binding),
                "answer": entry["answer"],
                "hasPrivateCard": bool(waiting or task_card),
                "cardRevision": card_revision,
            }
        if action == "card":
            if not waiting and not task_card:
                return {"available":False}
            try:
                async with open_host_session(self, work, lane="control") as (session, meta, level):
                    if channel_for(record["endpoint_key"]) == "telegram":
                        from .telegram_identity import verify_telegram_binding
                        if not await verify_telegram_binding(session, self.instance, key[0], meta, record["endpoint_key"]):
                            return {"available": False}
                    if task_card and level=="L3":
                        card = await present_coordinated(session,work,meta,task_card)
                    elif waiting and level in {"L2", "L3"} and (not waiting["task"] or level == "L3"):
                        card = await present_credential(session, work, meta, waiting["interaction"], task_id=waiting["task"])
                    else:
                        card = None
            except Exception:
                raise web.HTTPServiceUnavailable() from None
            return {**card,"cardRevision":card_revision} if card else {"available":False}
        if action == "result-view":
            if channel_for(record["endpoint_key"]) != "telegram" or not pending_query:
                return {"available": False}
            try:
                from .telegram_identity import verify_telegram_binding
                from .telegram_presentation import normalize_result_view
                async with open_host_session(self, work, lane="control") as (session, meta, level):
                    if level != "L3" or not await verify_telegram_binding(session, self.instance, key[0], meta, record["endpoint_key"]):
                        return {"available": False}
                    task_meta = {
                        **meta,
                        "io.agentbridge/task": {
                            "taskId": pending_query["taskId"],
                            "coordinatorLeaseVersion": str(pending_query["version"]),
                            "endpointId": work["endpoint"],
                            "conversationRef": work["conversation"],
                            "hostRunId": entry["run_id"],
                        },
                    }
                    issued = await session.call_tool(
                        "agentbridge_result_view",
                        {"operation": "issue", "task_id": pending_query["taskId"]},
                        meta=task_meta,
                    )
                if issued.isError:
                    return {"available": False}
                view = normalize_result_view(
                    (issued.structuredContent or {}).get("resultView"),
                    source_task_id=pending_query["taskId"],
                    endpoint_id=work["endpoint"],
                    allowed_origins=frozenset(self.card_origins),
                )
            except Exception:
                raise web.HTTPServiceUnavailable() from None
            if not view:
                return {"available": False}
            return {
                "available": True,
                "resultView": view,
                "count": pending_query["count"],
                "complete": pending_query["complete"],
            }
        if action == "cancel":
            stopped = await self.runs.cancel(entry["run_id"], **binding)
            return {"runId": entry["run_id"], "state": stopped["state"]}
        raise web.HTTPNotFound()


def create_app(spec: dict) -> web.Application:
    host = Host(spec)
    app = web.Application(client_max_size=18 * 1024 * 1024)
    app.router.add_post("/v1/{action:prepare-command|ready|accept|events|lookup|cancel|card|result-view|telegram-binding|telegram-route|telegram-present|telegram-enroll|telegram-timeline|telegram-media-source|telegram-media-append|telegram-received-source|skill-model|artifact-reissue|telegram-notifications|telegram-notification-receipt|telegram-chat-validate|telegram-chat-receipt|telegram-artifact-file|telegram-mirror-peers|telegram-task|telegram-watch-validate|telegram-workspace-result-view|telegram-workspace-media-v2}", host.handle)

    if host.coordinator:
        from .recovery import recover

        async def recovery_context(_app):
            job = asyncio.create_task(recover(host))
            try:
                yield
            finally:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        app.cleanup_ctx.append(recovery_context)

    async def close(_app):
        await host.commands.close()
        await host.runs.close()
        await host.mcp_pool.close()

    app.on_cleanup.append(close)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    spec = json.loads(private_text(parser.parse_args().config))
    os.umask(0o077)
    app = create_app(spec)
    web.run_app(app, host="127.0.0.1", port=int(spec["listenPort"]), access_log=None)


if __name__ == "__main__":
    main()
