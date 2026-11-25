"""Prepare files under the original Telegram run, keeping grants and bytes outside model history."""

import asyncio
import hashlib
import json
import re
from uuid import uuid4

SEARCH = "oa_certificate_search"
SINGLE = "oa_certificate_prepare_download"
BATCH = "oa_certificate_prepare_downloads"
EXPORTS = {"oa_addressbook_export", "smartlight_report_export"}
FILE_TOOLS = {SINGLE, BATCH, *EXPORTS}

DESCRIPTIONS = {
    SEARCH: "查找本人有权读取的证书，返回本次任务可用的 selection。存在多个版本时，请用户确认版本后再次查询。",
    SINGLE: "准备本次查询选中的一份证书。传入对应的 selection，中央将制品关联至原任务并交给原私聊投递。",
    BATCH: "准备本次查询选中的多份证书。将全部 selections 放在一次调用中，原私聊会逐份收到有效文件。",
}


def parameters(name, schema):
    if name in {SINGLE, BATCH}:
        key = "selection" if name == SINGLE else "selections"
        item = {"type": "string", "pattern": "^cert-[a-f0-9]{16}$"}
        value = (
            item
            if name == SINGLE
            else {"type": "array", "items": item, "minItems": 1, "maxItems": 20, "uniqueItems": True}
        )
        return {
            "type": "object",
            "properties": {key: value},
            "required": [key],
            "additionalProperties": False,
        }
    if name in EXPORTS or name == SEARCH:
        return {
            **schema,
            "properties": {k: v for k, v in schema["properties"].items() if k != "idempotency_key"},
            "required": [k for k in schema.get("required", []) if k != "idempotency_key"],
            "additionalProperties": False,
        }
    return schema


def _labels(values):
    return [
        s for s in values if isinstance(s, str) and len(s) <= 240 and "://" not in s and "/download/" not in s
    ]


class TaskFiles:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.selections = {}

    def _search_result(self, value, task_id):
        if value.get("status") != "succeeded" or not isinstance(value.get("result"), dict):
            return {"status": "unconfirmed"}
        result = value["result"]
        ambiguous = _labels(result.get("ambiguous_queries") or [])
        items = []
        selections = {}
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            entry = {
                k: v
                for k, v in item.items()
                if k
                in {
                    "title",
                    "filename",
                    "query",
                    "requested_version",
                    "document_type",
                    "display_size",
                    "match_kind",
                }
                and _labels([v])
            }
            raw = item.get("download_id")
            if (
                entry.get("query") not in ambiguous
                and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", raw or "")
                and entry.get("document_type") in {"patent_certificate", "software_copyright_certificate"}
            ):
                handle = "cert-" + uuid4().hex[:16]
                selections[handle] = (task_id, raw)
                entry["selection"] = handle
            items.append(entry)
        self.selections = selections
        ambiguities = [
            {"query": a["query"], "versions": _labels(a.get("versions") or [])}
            for a in result.get("ambiguities") or []
            if isinstance(a, dict) and a.get("query") in ambiguous
        ]
        return {
            "status": "succeeded",
            "items": items,
            "ambiguous_queries": ambiguous,
            "ambiguities": ambiguities,
            "unmatched_queries": _labels(result.get("unmatched_queries") or []),
        }

    def _prepare_arguments(self, name, arguments, task_id):
        key = "selection" if name == SINGLE else "selections"
        if set(arguments) != {key}:
            raise ValueError("Invalid certificate selection")
        handles = [arguments[key]] if name == SINGLE else arguments[key]
        if not isinstance(handles, list) or not 1 <= len(handles) <= 20 or len(set(handles)) != len(handles):
            raise ValueError("Invalid certificate selection")
        ids = []
        for handle in handles:
            source, download = self.selections[handle]
            if source != task_id:
                raise PermissionError("Certificate selection belongs to a different task")
            ids.append(download)
        return {"download_id": ids[0]} if name == SINGLE else {"download_ids": ids}

    async def execute(self, name, arguments, *, session, business_context, skill, binding):
        async with self.lock:
            meta = await business_context()
            task = meta.get("io.agentbridge/task") or {}
            task_id = task.get("taskId")
            if (
                not task_id
                or not task.get("coordinatorLeaseVersion")
                or task.get("endpointId") != binding.endpoint
                or task.get("conversationRef") != binding.conversation
            ):
                raise PermissionError("The originating Telegram task is unavailable")
            if name in {SINGLE, BATCH}:
                arguments = self._prepare_arguments(name, arguments, task_id)
            elif name in EXPORTS or name == SEARCH:
                if "idempotency_key" in arguments:
                    raise ValueError("File operation key must come from its host")
                # A retry of the same export in this task must not create a second file.
                # A fresh search must run normally so expired search grants can be replaced.
                if name in EXPORTS:
                    fingerprint = json.dumps(
                        [task_id, name, arguments], sort_keys=True, separators=(",", ":")
                    )
                    arguments = {
                        **arguments,
                        "idempotency_key": "tg-file-" + hashlib.sha256(fingerprint.encode()).hexdigest(),
                    }
            else:
                raise ValueError("Unknown file tool")
            if skill is not None and skill.selection:
                response = await skill.call(name, arguments)
            else:
                response = await session.call_tool(name, arguments=arguments, meta=meta)
            value = response.structuredContent or {}
            if response.isError or response.meta or value.get("interaction"):
                return response, {"status": "unconfirmed"}
            if name == SEARCH:
                return response, self._search_result(value, task_id)
            delivery = (
                value.get("result") if name in EXPORTS and value.get("status") == "succeeded" else value
            )
            if not isinstance(delivery, dict) or delivery.get("status") not in {"succeeded", "partial"}:
                return response, {"status": "unconfirmed"}
            many = name == BATCH
            schema = "agentbridge.document_delivery_batch.v1" if many else "agentbridge.document_delivery.v1"
            files = delivery.get("files") if many else [delivery.get("file")]
            expected = len(arguments["download_ids"]) if many else 1
            if (
                delivery.get("schemaVersion") != schema
                or not isinstance(files, list)
                or not 1 <= len(files) <= expected
                or any(not isinstance(f, dict) or not f.get("artifactId") for f in files)
            ):
                return response, {"status": "unconfirmed"}
            return response, {
                "status": "prepared" if len(files) == expected else "partial",
                "requestedCount": expected,
                "preparedCount": len(files),
                "meaning": "文件已在原任务备妥，附件送达情况以原私聊的投递回执为准。",
            }
