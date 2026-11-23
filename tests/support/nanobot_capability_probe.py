"""Two-user SDK catalog, admission boundaries and authenticated central file/notification gates."""

import asyncio
import json

from nanobot_agentbridge.worker import authenticated_session

from agentbridge.workspace.gateway import GatewayRequestError


class CatalogScenario:
    def __init__(self, expect_assigned, token):
        self.expect_assigned = expect_assigned
        self.token = token
        self.failures = []
        self.items = None
        self.calls = 0

    def reply(self, body):
        if self.token in json.dumps(body):
            raise ValueError("Private identity in model request")
        names = {item["function"]["name"] for item in body["tools"]}
        catalog = "mcp_ab_agentbridge_skill_catalog"
        expected = {
            catalog,
            "mcp_ab_agentbridge_server_profile",
            "mcp_ab_oa_session_status",
            "mcp_ab_oa_workflow_pending_list",
        }
        if catalog not in names or names - expected:
            raise ValueError("Unexpected model-facing capabilities")
        tools = [m for m in body["messages"] if m.get("role") == "tool"]
        if not tools:
            self.calls += 1
            return None, {
                "id": "read_my_catalog",
                "type": "function",
                "function": {"name": catalog, "arguments": "{}"},
            }
        raw = tools[-1]["content"]
        if not isinstance(raw, str):
            raise TypeError("Invalid skill response")
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(raw[index:])
            except ValueError:
                continue
            if not isinstance(payload, dict) or not isinstance(
                payload.get("items"), list
            ):
                continue
            self.items = [row["id"] for row in payload["items"]]
            if self.items != (["oa-work-log"] if self.expect_assigned else []):
                raise ValueError("Catalog scope did not match the central assignment")
            return (
                "我的目录有工作日志助手。" if self.items else "我的目录暂无分配。"
            ), None
        raise ValueError("Missing signed-in catalog")


async def capability_checks(gateway, bindings, scenarios, specs, instance, mcp_url):
    results = []
    for record, scenario, central in zip(bindings, scenarios, specs[:len(bindings)], strict=True):
        request_key = "my-skill-catalog-" + record["user_subject"]

        def query(record=record, request_key=request_key):
            events = list(
                gateway.send_dispatch(
                    {"idempotency_key": request_key},
                    record,
                    "列出我的业务助手。",
                    [],
                    None,
                )
            )
            if not events or events[-1]["state"] != "final":
                raise AssertionError("SDK catalog request did not finish")
            return events[-1]["text"]

        answer = await asyncio.to_thread(query)
        assert (
            scenario.items is not None and scenario.calls == 1 and not scenario.failures
        )
        for extra in (
            {"attachments": [{"name": "fixture.txt"}]},
            {"trial": {"id": "fixture"}},
        ):
            try:
                await asyncio.to_thread(
                    gateway._request,
                    "accept",
                    {
                        "requestKey": request_key + "-restricted",
                        "binding": record,
                        "message": "合成文件或技能试用",
                        **extra,
                    },
                )
            except GatewayRequestError as e:
                assert e.code == "NANOBOT_INVALID_REQUEST"
            else:
                raise AssertionError(
                    "Unverified attachment or trial reached host admission"
                )
        absent = await asyncio.to_thread(
            gateway.dispatch_evidence,
            {"idempotency_key": request_key + "-restricted"},
            record,
            request_key + "-restricted",
        )
        assert not absent["prompt_observed"]
        gated = {}
        payload = {
            "subject": record["user_subject"],
            "instance": instance,
            "tokenFile": central["tokenFile"],
            "mcpUrl": mcp_url,
        }
        async with authenticated_session(payload) as (session, meta, level):
            assert level == "L1"
            common = {"agent_host": "nanobot"}
            tools = {
                "agentbridge_host_notification_claim": {
                    **common,
                    "endpoint_key": record["endpoint_key"],
                    "limit": 1,
                },
                "agentbridge_host_artifact_reissue": {
                    **common,
                    "task_id": "00000000-0000-4000-8000-000000000001",
                    "artifact_id": "00000000-0000-4000-8000-000000000002",
                },
            }
            for name, args in tools.items():
                reply = await session.call_tool(name, args, meta=meta)
                public = " ".join(
                    item.text
                    for item in reply.content
                    if getattr(item, "type", None) == "text"
                )
                # Only a central host-level rejection establishes this boundary.
                assert reply.isError and "L3" in public, (
                    f"Missing L3 rejection for {name}"
                )
                gated[name] = "central L3 required"
        results.append(
            {
                "subject": record["user_subject"],
                "assigned": scenario.items,
                "answer": answer,
                "modelCatalogRestricted": True,
                "invalidAttachmentAndTrialRejectedBeforeAdmission": True,
                "centralGates": gated,
            }
        )
    return results
