"""Workspace transport for a configured NanoBot instance on a private loopback route."""

from __future__ import annotations

import http.client
import json
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from .gateway import GatewayRequestError


def _private_text(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("NanoBot configuration requires absolute paths")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("NanoBot route material must be owner-only")
        value = handle.read(1_048_577)
        if len(value) > 1_048_576:
            raise ValueError("NanoBot route material is too large")
        return value


def load_nanobot_route_specs() -> dict:
    filename = os.environ.get("AGENTBRIDGE_NANOBOT_ROUTES_FILE")
    if not filename:
        return {}
    document = json.loads(_private_text(Path(filename)))
    if not isinstance(document, dict) or not isinstance(document.get("routes"), dict):
        raise ValueError("Invalid NanoBot route configuration")
    routes = document["routes"]
    if any(not name or not isinstance(spec, dict) for name, spec in routes.items()):
        raise ValueError("Invalid NanoBot route specification")
    return routes


class NanoBotGatewayClient:
    def __init__(self, *, url: str, token_file: Path, instance_id: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not instance_id
        ):
            raise ValueError(
                "NanoBot routes require an explicit IPv4 loopback port and instance ID"
            )
        self.port = parsed.port
        self.token_file = token_file
        self.instance_id = instance_id

    @classmethod
    def from_environment(cls) -> dict[str, NanoBotGatewayClient]:
        routes = load_nanobot_route_specs()
        return {
            name: cls(
                url=spec["url"],
                token_file=Path(spec["tokenFile"]),
                instance_id=spec["instanceId"],
            )
            for name, spec in routes.items()
        }

    def _request(self, action: str, payload: dict, *, timeout=8) -> dict:
        try:
            token = _private_text(self.token_file).strip()
            if (
                len(token) < 32
                or not token.isascii()
                or any(c.isspace() for c in token)
            ):
                raise ValueError("Invalid service credential")
        except (OSError, ValueError):
            raise GatewayRequestError(
                "NANOBOT_ROUTE_CONFIGURATION", "宿主连接凭证不可用。", {"stage": "bind"}
            ) from None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            try:
                connection.connect()
            except OSError:
                raise GatewayRequestError(
                    "GATEWAY_CONNECTION_FAILED", "无法连接宿主。", {"stage": "connect"}
                ) from None
            connection.request(
                "POST",
                "/v1/" + action,
                body=json.dumps({"instance": self.instance_id, **payload}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                },
            )
            response = connection.getresponse()
            data = response.read(2_097_153)
            if response.status >= 500 or len(data) > 2_097_152:
                raise GatewayRequestError(
                    "GATEWAY_RESPONSE_INVALID",
                    "无法核对宿主收据。",
                    {"stage": "request"},
                )
            if response.status != 200:
                code = {
                    401: "NANOBOT_INSTANCE_UNAUTHORIZED",
                    403: "NANOBOT_BINDING_UNAVAILABLE",
                    409: "NANOBOT_DISPATCH_CONFLICT",
                    400: "NANOBOT_INVALID_REQUEST",
                }.get(response.status, "NANOBOT_REQUEST_FAILED")
                raise GatewayRequestError(code, "宿主未接受此请求。", {"stage": "bind"})
            result = json.loads(data)
            if (
                not isinstance(result, dict)
                or result.get("instance") != self.instance_id
            ):
                raise ValueError("Unexpected host reply")
            return result
        except (TimeoutError, OSError, http.client.HTTPException, ValueError):
            raise GatewayRequestError(
                "GATEWAY_CONNECTION_CLOSED", "宿主收据待核对。", {"stage": "request"}
            ) from None
        finally:
            connection.close()

    def skill_completion(self, binding, system, prompt):
        result = self._request("skill-model", {
            "binding": self._binding(binding), "system": system, "prompt": prompt,
        }, timeout=100)
        if (result.get("execution") != "tool_free_completion" or not isinstance(result.get("text"), str)
                or not result["text"].strip() or len(result["text"]) > 48_000):
            raise GatewayRequestError("SKILL_MODEL_UNAVAILABLE", "技能模型未返回完整文本。")
        return {k: result.get(k) for k in ("text", "model", "usage", "execution")}

    def readiness(self) -> dict:
        try:
            result = self._request("ready", {})
            if result.get("available") is not True:
                return {
                    "available": False,
                    "recoverable": False,
                    "code": "NANOBOT_BINDINGS_UNAVAILABLE",
                }
        except GatewayRequestError as exc:
            return {
                "available": False,
                "recoverable": exc.code.startswith("GATEWAY_"),
                "code": exc.code,
            }
        capabilities = result.get("capabilities", ["chat"])
        if not isinstance(capabilities,list):
            capabilities = []
        return {"available": True, "recoverable": False, "code": None,
                "adapterVersion":result.get("adapterVersion"),
                "capabilities":[name for name in ("chat","credential_card","attachments","skill_trial","skills","skill_model") if name in capabilities]}

    @staticmethod
    def _binding(binding: dict) -> dict:
        return {
            key: binding[key]
            for key in (
                "user_subject",
                "endpoint_id",
                "endpoint_key",
                "session_key",
                "credential_ref",
            )
        }

    def resolve_telegram_sender(self, bot_id: int, sender: int) -> dict | None:
        result = self._request("telegram-route", {"botId":str(bot_id), "senderId":str(sender)})
        return self._binding(result["binding"]) if isinstance(result.get("binding"),dict) else None

    def confirm_telegram_enrollment(self, bot_id, sender, code):
        return self._request("telegram-enroll",{"botId":str(bot_id),"senderId":str(sender),"code":code}).get("confirmed") is True

    def present_telegram_challenge(self, bot_id: int, sender: int, code: str) -> str | None:
        return self._request("telegram-present",{"botId":str(bot_id),"senderId":str(sender),"code":code}).get("confirmationCode")

    def reissue_artifact(self, binding, dispatch, task_id, artifact_id):
        receipt = self._request("artifact-reissue", {
            "binding": self._binding(binding), "requestKey": dispatch["idempotency_key"],
            "runId": dispatch["accepted_run_id"], "taskId": task_id, "artifactId": artifact_id,
        })
        if receipt.get("taskId") != task_id or receipt.get("artifactId") != artifact_id:
            return "unavailable"
        if receipt.get("renewed") is True:
            return "renewed"
        return "owner_required" if receipt.get("signedInOwnerRequired") is True else "unavailable"

    def prepare_command(self, binding: dict, task_id: str, command_key: str) -> None:
        receipt = self._request("prepare-command", {
            "binding": self._binding(binding), "taskId": task_id, "commandKey": command_key,
        })
        if (receipt.get("accepted") is not True or receipt.get("taskId") != task_id
                or receipt.get("commandKey") != command_key):
            raise GatewayRequestError("GATEWAY_RESPONSE_INVALID", "准备请求的回执待核对。")

    def verify_telegram_binding(self, binding: dict) -> bool:
        reply = self._request("telegram-binding", {"binding": self._binding(binding)})
        return reply.get("verified") is True

    def send_dispatch(
        self, dispatch: dict, binding: dict, message: str, attachments, trial
    ):
        if attachments or trial:
            allowed = self.readiness().get("capabilities", [])
        if (attachments and "attachments" not in allowed) or (trial and "skill_trial" not in allowed):
            raise GatewayRequestError(
                "NANOBOT_CAPABILITY_UNAVAILABLE",
                "此宿主尚未开放附件或技能试用。",
                {"stage": "bind"},
            )
        payload = {
            "binding": self._binding(binding),
            "requestKey": dispatch["idempotency_key"],
        }
        if trial:
            fields = ("dispatch_id", "draft_id", "expected_revision", "profile")
            payload["trial"] = {field: trial[field] for field in fields}
        result = self._request("accept", {**payload, "message": message, **({"attachments":attachments} if attachments else {})})
        run_id = result.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise GatewayRequestError(
                "GATEWAY_RESPONSE_INVALID", "缺少宿主运行收据。", {"stage": "request"}
            )
        yield {"type": "accepted", "runId": run_id, "status": "accepted"}
        if result.get("state") == "completed":
            observed = self._request("lookup", payload)
            if observed.get("runId") != run_id or observed.get("state") != "completed" or not isinstance(observed.get("answer"),str):
                raise GatewayRequestError("GATEWAY_RESPONSE_INVALID","既有运行的权威结果待核对。")
            yield {"type":"chat","state":"final","runId":run_id,"text":observed["answer"]}
            return
        cursor = 0
        last_card_revision = 0
        text = ""
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            page = self._request(
                "events", {**payload, "runId": run_id, "after": cursor}
            )
            revision=page.get("cardRevision",1) if page.get("hasPrivateCard") else 0
            if type(revision) is not int or revision<0:
                raise GatewayRequestError("GATEWAY_RESPONSE_INVALID","宿主返回的可信卡版本无效。")
            if revision and revision!=last_card_revision:
                yield {"type":"progress","kind":"private_interaction","runId":run_id,
                       "status":"waiting_private","cardRevision":revision,"text":"请在本人工作台打开可信卡。"}
            if last_card_revision and not revision:
                yield {"type":"progress","kind":"private_interaction","runId":run_id,
                       "status":"closed","cardRevision":last_card_revision}
            last_card_revision = revision
            for event in page["events"]:
                cursor = event["sequence"]
                if event["kind"] == "delta":
                    text += event["text"]
                    yield {
                        "type": "chat",
                        "state": "delta",
                        "runId": run_id,
                        "text": text,
                    }
                elif event["kind"] == "completed":
                    yield {
                        "type": "chat",
                        "state": "final",
                        "runId": run_id,
                        "text": page["answer"],
                    }
                    return
                elif event["kind"] == "stopped":
                    yield {
                        "type": "chat",
                        "state": "aborted",
                        "runId": run_id,
                        "text": "已停止回复。",
                    }
                    return
                elif event["kind"] == "unknown":
                    raise GatewayRequestError(
                        "GATEWAY_CONNECTION_CLOSED",
                        "原运行结果待核对。",
                        {"stage": "observe"},
                    )
            if len(page["events"]) < 200:
                time.sleep(0.25)
        raise GatewayRequestError(
            "GATEWAY_TIMEOUT", "等待宿主答复超时。", {"stage": "observe"}
        )

    def dispatch_evidence(self, dispatch: dict, binding: dict, run_ref: str) -> dict:
        result = self._request(
            "lookup",
            {
                "binding": self._binding(binding),
                "requestKey": dispatch["idempotency_key"],
            },
        )
        if not result.get("runId") or run_ref not in (
            result["runId"],
            dispatch["idempotency_key"],
        ):
            return {"prompt_observed": False}
        return {
            "prompt_observed": True,
            "observed_run_id": result["runId"],
            "state": result["state"],
            "has_private_card":bool(result.get("hasPrivateCard")),
            "card_revision":result.get("cardRevision",0),
            "final_text": result.get("answer")
            if result["state"] == "completed"
            else None,
        }

    def private_card(self, dispatch: dict, binding: dict, run_ref: str) -> dict:
        result = self._request("card",{"binding":self._binding(binding),
            "requestKey":dispatch["idempotency_key"],"runId":run_ref})
        if result.get("available") is not True:
            return {"available":False}
        parsed = urlsplit(result["url"])
        if (result.get("kind") not in {"credential","business_input","execution_authorization"} or parsed.scheme!="https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise GatewayRequestError("GATEWAY_RESPONSE_INVALID","可信卡地址无效。")
        return {"available":True,"kind":result["kind"],"url":result["url"],"cardRevision":result.get("cardRevision",1)}
