"""Workspace transport for hosted adapters on authenticated private loopback routes."""

from __future__ import annotations

import http.client
import json
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

from .gateway import GatewayRequestError
from .host_instances import _alias, _label


def _private_text(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("Host configuration requires absolute paths")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("Host route material must be owner-only")
        value = handle.read(1_048_577)
        if len(value) > 1_048_576:
            raise ValueError("Host route material is too large")
        return value


def _load_route_specs(variable: str) -> dict:
    filename = os.environ.get(variable)
    if not filename:
        return {}
    document = json.loads(_private_text(Path(filename)))
    if not isinstance(document, dict) or not isinstance(document.get("routes"), dict):
        raise ValueError("Invalid host route configuration")
    routes = document["routes"]
    if any(not name or not isinstance(spec, dict) for name, spec in routes.items()):
        raise ValueError("Invalid host route specification")
    return routes


def load_nanobot_route_specs() -> dict:
    return _load_route_specs("AGENTBRIDGE_NANOBOT_ROUTES_FILE")


def load_pi_route_specs() -> dict:
    return _load_route_specs("AGENTBRIDGE_PI_ROUTES_FILE")


def route_specs_for(host_type: str) -> dict:
    if host_type == "nanobot":
        return load_nanobot_route_specs()
    if host_type == "pi":
        return load_pi_route_specs()
    return {}


def route_subjects(spec: dict) -> dict:
    subjects = spec.get("subjects", {})
    if not isinstance(subjects, dict):
        raise ValueError("invalid principal mapping")
    for subject, ref in subjects.items():
        _label(subject, "user subject", 256)
        _alias(ref, "credential")
    if len(set(subjects.values())) != len(subjects):
        raise ValueError("each principal requires a distinct personal credential reference")
    return subjects


def pi_telegram_sender(spec: dict, subject: str, bot=None) -> str | None:
    """A Pi Telegram route requires exactly one separately assigned private sender."""
    telegram = spec.get("telegram")
    subjects = spec.get("subjects")
    if not isinstance(telegram, dict) or not isinstance(subjects, dict) or subject not in subjects:
        return None
    senders = telegram.get("allowedSenders")
    if not isinstance(senders, dict) or set(senders) != {subject}:
        return None
    from .telegram_pilot_links import numeric_id
    try:
        configured_bot = numeric_id(telegram.get("botId"))
        sender = senders[subject]
        if not isinstance(sender, str) or numeric_id(sender) != sender:
            return None
        if bot is not None and numeric_id(bot) != configured_bot:
            return None
        return sender
    except ValueError:
        return None


class NanoBotGatewayClient:
    host_type = "nanobot"

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
                "Host routes require an explicit IPv4 loopback port and instance ID"
            )
        self.port = parsed.port
        self.token_file = token_file
        self.instance_id = instance_id

    @classmethod
    def from_spec(cls, spec):
        return cls(url=spec["url"], token_file=Path(spec["tokenFile"]), instance_id=spec["instanceId"])

    @classmethod
    def from_environment(cls) -> dict[str, NanoBotGatewayClient]:
        return {name: cls.from_spec(spec) for name, spec in route_specs_for(cls.host_type).items()}

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
                self.host_type.upper() + "_ROUTE_CONFIGURATION", "宿主连接凭证不可用。", {"stage": "bind"}
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
            # A private file read carries one task artifact (32 MiB maximum) as base64.
            limit = 45 * 1024 * 1024 if action == "telegram-artifact-file" else 2_097_152
            if action in {"telegram-workspace-media","telegram-workspace-media-v2"}:
                limit = 9 * 1024 * 1024
            data = response.read(limit + 1)
            if response.status >= 500 or len(data) > limit:
                raise GatewayRequestError(
                    "GATEWAY_RESPONSE_INVALID",
                    "无法核对宿主收据。",
                    {"stage": "request"},
                )
            if response.status != 200:
                reason = {401: "INSTANCE_UNAUTHORIZED", 403: "BINDING_UNAVAILABLE",
                          409: "DISPATCH_CONFLICT", 400: "INVALID_REQUEST"}.get(response.status, "REQUEST_FAILED")
                code = self.host_type.upper() + "_" + reason
                raise GatewayRequestError(code, "宿主未接受此请求。", {"stage": "bind"})
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("instance") != self.instance_id:
                raise ValueError("Unexpected host reply")
            if self.host_type == "pi" and result.get("hostType") != self.host_type:
                raise ValueError("Unexpected host type")
            if self.host_type == "pi" and action != "ready" and result.get("adapterVersion") != self.adapter_version:
                raise GatewayRequestError("PI_ADAPTER_VERSION_MISMATCH", "Pi 返回的适配器版本与部署配置不符。",
                                          {"stage": "request"})
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
            available = result.get("available") is True
            if not available and self.host_type != "pi":
                return {
                    "available": False,
                    "recoverable": False,
                    "code": self.host_type.upper() + "_BINDINGS_UNAVAILABLE",
                }
        except GatewayRequestError as exc:
            return {
                "available": False,
                "recoverable": exc.code.startswith("GATEWAY_"),
                "code": exc.code,
            }
        capabilities = result.get("capabilities", ["chat"]) if available else []
        if not isinstance(capabilities,list):
            capabilities = []
        status = {"available": available, "recoverable": False,
                  "code": None if available else "PI_BINDINGS_UNAVAILABLE",
                  "adapterVersion": result.get("adapterVersion"),
                  "capabilities": [name for name in ("chat", "credential_card", "attachments", "skill_trial", "skills", "skill_model")
                                   if name in capabilities]}
        if self.host_type == "pi":
            if result.get("sdkVersion") != "1.6.0" or result.get("acceptedLevel") not in ("L1", "L2", "L3"):
                return {"available": False, "recoverable": False, "code": "PI_COMPATIBILITY_MISMATCH"}
            status.update(sdkVersion=result["sdkVersion"], acceptedLevel=result["acceptedLevel"])
            configured, verified = result.get("configuredBindings"), result.get("verifiedBindings")
            if type(configured) is int and type(verified) is int and 0 <= verified <= configured:
                status.update(configuredBindings=configured, verifiedBindings=verified)
            fields = {"modelConfiguration": ("configured",), "centralConnection": ("verified", "partial", "unavailable"),
                      "modelInvocation": ("unverified", "last_succeeded", "last_unconfirmed")}
            status.update({name: result[name] for name, values in fields.items() if result.get(name) in values})
        return status

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

    def publish_telegram_timeline(self, binding, message, role, text, run_id, receipts,
                                  *, projection_mode=None, source_state=None):
        result = self._request("telegram-timeline", {
            "binding": self._binding(binding), "messageId": message, "role": role, "text": text, "runId": run_id,
            "deliveredMessageIds": receipts,
            **({"projectionMode": projection_mode} if projection_mode is not None else {}),
            **({"sourceState": source_state} if source_state is not None else {}),
        })
        if result.get("recorded") is not True:
            raise GatewayRequestError("GATEWAY_RESPONSE_INVALID", "聊天记录回执待核对。")

    def send_dispatch(
        self, dispatch: dict, binding: dict, message: str, attachments, trial
    ):
        if attachments or trial:
            allowed = self.readiness().get("capabilities", [])
        if (attachments and "attachments" not in allowed) or (trial and "skill_trial" not in allowed):
            raise GatewayRequestError(
                self.host_type.upper() + "_CAPABILITY_UNAVAILABLE",
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
                if self.host_type == "pi" and event["kind"] in {"tool_started", "tool_finished"}:
                    # Raw tool arguments and provider diagnostics never enter public progress events.
                    started = event["kind"] == "tool_started"
                    yield {"type": "progress", "kind": "tool", "runId": run_id,
                           "phase": "start" if started else "result",
                           "label": "正在调用业务工具" if started else "业务工具已返回"}
                elif event["kind"] == "delta":
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
                        **({"safeToRetry": False} if self.host_type == "pi" else {}),
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
            "has_private_card": bool(result.get("hasPrivateCard")),
            "card_revision": result.get("cardRevision", 0),
            "has_result_view": bool(result.get("hasResultView")),
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
        return {
            "available": True,
            "kind": result["kind"],
            "url": result["url"],
            "cardRevision": result.get("cardRevision", 1),
            **({"interaction": result["interaction"]} if isinstance(result.get("interaction"), dict) else {}),
        }

    result_view_timeout = 8

    def result_view(self, dispatch: dict, binding: dict, run_ref: str) -> dict:
        result = self._request("result-view", {
            "binding": self._binding(binding),
            "requestKey": dispatch["idempotency_key"],
            "runId": run_ref,
        }, timeout=self.result_view_timeout)
        if result.get("available") is not True or not isinstance(result.get("resultView"), dict):
            return {"available": False}
        view = result["resultView"]
        parsed = urlsplit(str(view.get("url") or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise GatewayRequestError("GATEWAY_RESPONSE_INVALID", "待办办理入口地址无效。")
        return {
            "available": True,
            "resultView": view,
            "count": result.get("count"),
            "complete": bool(result.get("complete")),
        }


class PiGatewayClient(NanoBotGatewayClient):
    host_type = "pi"
    # Result issuance verifies the original central task and may take longer than normal host reads.
    result_view_timeout = 45

    def __init__(self, *, url, token_file, instance_id, adapter_version):
        super().__init__(url=url, token_file=token_file, instance_id=instance_id)
        if not isinstance(adapter_version, str) or not adapter_version.strip():
            raise ValueError("Pi adapter version required")
        self.adapter_version = adapter_version

    @classmethod
    def from_spec(cls, spec):
        return cls(url=spec["url"], token_file=Path(spec["tokenFile"]), instance_id=spec["instanceId"],
                   adapter_version=spec["adapterVersion"])

    def readiness(self):
        result = super().readiness()
        if "adapterVersion" in result and result["adapterVersion"] != self.adapter_version:
            return {"available": False, "recoverable": False, "code": "PI_ADAPTER_VERSION_MISMATCH"}
        return result

    def stop_generation(self, binding, dispatch):
        run_id = dispatch["accepted_run_id"]
        receipt = self._request("cancel", {"binding": self._binding(binding),
            "requestKey": dispatch["idempotency_key"], "runId": run_id})
        if receipt.get("runId") != run_id or receipt.get("state") not in {"stopped", "completed", "unknown"}:
            raise GatewayRequestError("GATEWAY_RESPONSE_INVALID", "停止回复的原运行回执待核对。")
        return receipt["state"]


class WorkspacePiGatewayClient(PiGatewayClient):
    """Resolve a deployed Pi version at the point of using the original Workspace binding."""

    def for_binding(self, binding, version):
        try:
            routes = load_pi_route_specs()
            spec = routes[binding["route_ref"]]
            subjects = route_subjects(spec)
            if not isinstance(spec.get("url"), str):
                raise ValueError("Pi route URL required")
            current = PiGatewayClient.from_spec(spec)
            if (
                current.instance_id != self.instance_id
                or current.adapter_version != version
                or current.port != self.port
                or current.token_file != self.token_file
                or not current.token_file.is_absolute()
                or subjects[binding["user_subject"]] != binding["credential_ref"]
                or sum(row.get("instanceId") == self.instance_id for row in routes.values()) != 1
            ):
                raise ValueError("Pi route does not match the registered Workspace binding")
        except (OSError, KeyError, TypeError, ValueError):
            raise GatewayRequestError("HOST_BINDING_UNAVAILABLE", "原 Pi 实例的受控连接与账号绑定不符。",
                                      {"stage": "bind"}) from None
        return self if current.adapter_version == self.adapter_version else current


def configured_adapters():
    """Yield the same reviewed manifest entries to both the runtime and admin catalog."""
    aliases = {"openclaw-default"}
    instances = {"openclaw-default"}
    for client_type in (NanoBotGatewayClient, WorkspacePiGatewayClient):
        for alias, spec in route_specs_for(client_type.host_type).items():
            instance = spec["instanceId"]
            if alias in aliases or instance in instances:
                raise ValueError("Duplicate Workspace route or instance")
            aliases.add(alias)
            instances.add(instance)
            yield alias, spec, client_type.from_spec(spec)
