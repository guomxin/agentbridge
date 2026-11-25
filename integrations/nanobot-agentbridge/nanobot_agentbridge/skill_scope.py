"""Scope business tools and independent trial runs to verified central skill revisions."""
import json
from pathlib import PurePosixPath
from uuid import UUID


def valid_trial(value, message) -> bool:
    if not isinstance(value,dict) or set(value)!={"dispatch_id","draft_id","expected_revision","profile"}:
        return False
    try:
        return (all(isinstance(value[k],str) and str(UUID(value[k]))==value[k] for k in ("dispatch_id","draft_id"))
                and type(value["expected_revision"]) is int and value["expected_revision"]>0
                and isinstance(value["profile"],str) and 0 < len(value["profile"].strip()) <=128
                and len(message)<=3000)
    except ValueError:
        return False


def sample_resources(bundle, profile):
    manifest = bundle["manifest"]
    if profile not in manifest["profiles"]:
        raise ValueError("草稿模式不匹配")
    resources = bundle["resources"]
    paths = [
        manifest["entrypoint"],
        *manifest.get("required_resources", {}).get(
            profile, manifest.get("resources", [])
        ),
    ]
    if any(
        not isinstance(resources.get(path), str)
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        for path in paths
    ):
        raise ValueError("草稿必读资料不完整")
    return {path: resources[path] for path in paths}


async def run_trial(session, request, meta, complete):
    trial = request["trial"]
    dispatch = str(UUID(trial["dispatch_id"]))
    model_prompt = request["message"]
    data = {key: trial[key] for key in ("draft_id", "expected_revision", "profile")}
    data.update(prompt=model_prompt, request_key="workspace-sample:" + dispatch)
    origin = {
        "endpointId": request["endpoint"],
        "endpointKey": request["endpointKey"],
        "conversationRef": request["conversation"],
        "requestKey": request["requestKey"],
    }
    stamp = {
        **meta,
        "agentbridge/skill-trial": {"dispatchId": dispatch},
        "io.agentbridge/skill-origin": origin,
    }
    start = await session.call_tool(
        "agentbridge_skill_authoring", {"action": "test", "data": data}, meta=stamp
    )
    receipt = start.structuredContent or {}
    if (
        start.isError
        or receipt.get("mode") != "isolated_sample"
        or receipt.get("prompt") != model_prompt
        or receipt.get("status") != "running"
    ):
        raise PermissionError("中央未核验原试用派发")
    prompt = json.dumps({
        "instruction": receipt["instruction"], "profile": trial["profile"],
        "resources": sample_resources(receipt["bundle"], trial["profile"]), "sample": model_prompt,
    }, ensure_ascii=False)
    answer = await complete(prompt)
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 16000:
        raise ValueError("模型样例缺少可提交的结果")
    finished = await session.call_tool(
        "agentbridge_skill_authoring",
        {
            "action": "test_result",
            "data": {"test_id": receipt["test_id"], "output": answer},
        },
        meta=stamp,
    )
    saved = finished.structuredContent or {}
    if (
        finished.isError
        or saved.get("test_id") != receipt["test_id"]
        or saved.get("status") != "reported"
    ):
        raise ValueError("原样例的中央收据尚未核对")
    return answer


class SkillRun:
    """Bind tool calls in one host run to the current central task and lease."""

    def __init__(self, session, get_task_context):
        import asyncio

        self.session = session
        self.task = get_task_context
        self.lock = asyncio.Lock()
        self.selection = None
        self.binding = None
        self.allowed = None
        self.pending = False
        self.capabilities = set()
        self.effects = set()
        self.allow_batch = False
        self.source = None

    def private_prepare_scope(self, capability):
        if (
            not self.binding
            or self.pending
            or capability not in self.capabilities
            or "controlled_write" not in self.effects
            and "reversible_write" not in self.effects
            or ".batch." in capability
            and not self.allow_batch
        ):
            return None
        skill, profile, version, _ = self.selection
        return {
            "skill_id": skill,
            "profile": profile,
            "expected_version": version,
            "source_id": self.source,
            "binding_id": self.binding,
            "prepare": capability,
        }

    def permits(self, name):
        if self.pending or self.selection is not None:
            if name == "agentbridge_task_plan_prepare":
                return bool(
                    self.binding
                    and not self.pending
                    and self.capabilities
                    and "read" in self.effects
                )
            if name in {
                "agentbridge_server_profile",
                "agentbridge_skill_catalog",
                "agentbridge_skill_get",
            }:
                return True
            return (
                not self.pending and self.allowed is not None and name in self.allowed
            )
        return True

    async def call(self, name, arguments):
        from mcp.types import CallToolResult, TextContent

        def reject():
            return CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="此次技能模式未开放该调用。")],
            )

        async with self.lock:
            if not self.permits(name):
                return reject()
            if name in {"agentbridge_skill_catalog", "agentbridge_server_profile"}:
                return await self.session.call_tool(name, arguments)
            if name != "agentbridge_skill_get" and not self.selection:
                return await self.session.call_tool(name, arguments)
            if name == "agentbridge_skill_get":
                requested = tuple(
                    arguments.get(field)
                    for field in (
                        "skill_id",
                        "profile",
                        "expected_version",
                        "source_id",
                    )
                )
                if (
                    not all(isinstance(v, str) and v for v in requested[:3])
                    or self.selection
                    and self.selection != requested
                ):
                    return reject()
                self.selection = requested
                self.pending = True
            meta = await self.task()
            if not meta.get("io.agentbridge/task", {}).get("coordinatorLeaseVersion"):
                return reject()
            if self.binding:
                meta = {**meta, "agentbridge/skill": {"bindingId": self.binding}}
            if name in {"database_execute", "database_capabilities"}:
                capability = arguments.get("capability")
                if (
                    not self.source
                    or arguments.get("source_id") not in {None, self.source}
                    or (capability is not None and capability not in self.capabilities)
                    or arguments.get("after_source_id") is not None
                ):
                    return reject()
                if name == "database_execute":
                    if capability not in self.capabilities:
                        return reject()
                    if (
                        capability == "database.report.export"
                        and (arguments.get("arguments") or {}).get("query_capability")
                        not in self.capabilities
                    ):
                        return reject()
                arguments = {**arguments, "source_id": self.source}
                result = await self.session.call_tool(name, arguments, meta=meta)
                if (
                    name == "database_capabilities"
                    and not capability
                    and not result.isError
                ):
                    fields = result.structuredContent or {}
                    public = {
                        "sources": [
                            {
                                "source_id": self.source,
                                "capabilities": [
                                    c
                                    for c in src.get("capabilities", [])
                                    if c.get("name") in self.capabilities
                                ],
                            }
                            for src in fields.get("sources", [])
                            if src.get("source_id") == self.source
                        ]
                    }
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text", text=json.dumps(public, ensure_ascii=False)
                            )
                        ],
                        structuredContent=public,
                    )
                return result
            result = await self.session.call_tool(name, arguments, meta=meta)
            if name != "agentbridge_skill_get":
                return result
            fields = result.structuredContent or {}
            if (
                result.isError
                or fields.get("status") != "succeeded"
                or (
                    fields.get("skill_id"),
                    fields.get("profile"),
                    fields.get("version"),
                )
                != self.selection[:3]
                or not isinstance(fields.get("binding_id"), str)
                or self.binding
                and fields["binding_id"] != self.binding
            ):
                return reject()
            self.binding = fields["binding_id"]
            execution = fields["execution"]
            capabilities = execution.get("capabilities")
            self.allowed = set()
            self.source = (fields.get("settings") or {}).get("source_id") or None
            self.effects = set(execution.get("effects", []))
            self.allow_batch = execution.get("allow_batch") is True
            if isinstance(capabilities, list):
                self.capabilities = {c for c in capabilities if isinstance(c, str)}
                if "database.report.export" in self.capabilities:
                    self.capabilities.add("database.report.download")
                if self.source and any(
                    c.startswith("database.") for c in self.capabilities
                ):
                    self.allowed.update({"database_capabilities", "database_execute"})
                self.allowed.update(
                    c.replace(".", "_") for c in capabilities if isinstance(c, str)
                )
                self.allowed.update(
                    c.split(".")[0] + "_session_status"
                    for c in capabilities
                    if isinstance(c, str)
                )
            self.pending = False
            public = {
                k: fields[k]
                for k in (
                    "skill_id",
                    "version",
                    "profile",
                    "loaded_resources",
                    "content",
                    "execution",
                    "status",
                )
            }
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text=json.dumps(public, ensure_ascii=False)
                    )
                ],
                structuredContent=public,
            )
