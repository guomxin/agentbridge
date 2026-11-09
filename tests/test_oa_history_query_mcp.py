"""Real history receipts retain whole rows/cursors through MCP and native output."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from agentbridge.adapters.base import AdapterLoginRequired, AdapterSessionCheckUnavailable
from agentbridge.core.oa_history_queries import OaHistoryQueryError, response_chars
from agentbridge.core.oa_history_query_service import OaHistoryQueryService
from tests.authorization_fixtures import authorized_service
from tests import test_central_mcp as mcp_fixtures
from tests import test_oa_history_query_service as history_fixtures


class OaHistoryQueryMcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="oa-history-mcp-")
        self.addCleanup(self.temporary.cleanup)
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.capture_session_state.return_value = {"cookies": []}
        self.service = authorized_service(home=self.temporary.name,
            base_url="http://oa.example.test/seeyon/", worker_factory=lambda *_: worker)
        self.activate()
        self.rows = []
        def read_page(_adapter, _worker, *, page_number, **_kwargs):
            return {"total": len(self.rows), "pages": (len(self.rows) + 49) // 50,
                "page": page_number, "size": 50, "items": deepcopy(self.rows[(page_number-1)*50:page_number*50])}
        def read_nodes(*_args, **_kwargs):
            raise ValueError("Synthetic missing node evidence")
        self.service.oa_history_query_service = OaHistoryQueryService(
            self.service.oa_history_queries, read_page=read_page, read_nodes=read_nodes)
        self.sequence = 0

    def activate(self):
        session = self.service.sessions.get_or_create(user_subject="user-a", system_id="oa", expected_principal_ref="Alice")
        session = self.service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
        self.service.session_states.save(session["session_id"], {"cookies": []})

    def invoke(self, **kwargs):
        self.sequence += 1
        with patch("agentbridge.core.tasks._utc_now", return_value="2026-11-09T01:00:00.123456+00:00"):
            task = self.service.ensure_host_task(user_subject="user-a", token_id="test-token",
                agent_host="test-host", host_task_key=f"mcp-query-{self.sequence}",
                endpoint_key="test-endpoint", client_type="web", external_subject="user-a",
                conversation_ref="test-conversation", title="历史查询")
        self.last_response = self.service.invoke(**{**kwargs, "task_id": task["task"]["taskId"]})
        return self.last_response

    def request(self, client, token, kind, arguments):
        response = mcp_fixtures.CentralMcpTests._request(client, "tools/call",
            request_id="d2a42300-b5aa-415b-8e8d-a0e79ee120b0", token=token,
            params={"name": f"oa_workflow_history_{kind}", "arguments": arguments})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def test_whole_row_boundary_is_compact_through_real_mcp_and_native(self):
        self.rows = [history_fixtures.row(i, ended=False, title="中" * 10) for i in range(3)]
        for row in self.rows:
            row["current_handlers"] = [chr(0x4e00+i) for i in range(310)]
        with mcp_fixtures.CentralMcpFixture() as (service, _store, token, client):
            service.invoke.side_effect = self.invoke
            first = self.request(client, token, "query", {"collection": "done", "temporal": {"kind": "all"}})
            self.assertFalse(first["isError"])
            payload = first["structuredContent"]["result"]
            self.assertEqual(len(payload["items"]), 2)
            self.assertEqual(payload["state"], "partial")
            self.assertGreater(response_chars(first["structuredContent"]), 12000)
            self.assertLessEqual(response_chars(payload), 10000)
            cursor = payload["next_cursor"]
            replay = self.request(client, token, "next", {"cursor": payload["replay_cursor"]})
            self.assertEqual(replay["structuredContent"]["result"], payload)
            next_page = self.request(client, token, "next", {"cursor": cursor})
            self.assertEqual(len(next_page["structuredContent"]["result"]["items"]), 1)
            listed = mcp_fixtures.CentralMcpTests._request(client, "tools/list", request_id=2, token=token).json()
            for tool in listed["result"]["tools"]:
                if tool["name"] in {"oa_workflow_history_query", "oa_workflow_history_next"}:
                    self.assertEqual(tool["outputSchema"]["type"], "object")
                    self.assertTrue(tool["annotations"]["readOnlyHint"])
        fixtures = []
        for kind, response in (("query", first), ("next", replay), ("next", next_page)):
            text = response["content"][0]["text"]
            self.assertEqual(json.loads(text), response["structuredContent"])
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 12000)
            fixtures.append({"kind": kind, "result": response})
        node = shutil.which("node")
        self.assertIsNotNone(node, "Native boundary check requires the registered Node test runtime")
        root = Path(__file__).resolve().parents[1]
        script = """
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {createAgentBridgeProxyTools} from './integrations/openclaw-agentbridge/lib/proxy-tools.js';
import {InteractionCoordinator} from './integrations/openclaw-agentbridge/lib/coordinator.js';
import {resolvePluginConfig} from './integrations/openclaw-agentbridge/lib/config.js';
for (const fixture of JSON.parse(fs.readFileSync(0,'utf8'))) {
 const context={sessionKey:'agent:main:agentbridge-workspace:direct:test-user',runId:'test-run'};
 const coordinator=new InteractionCoordinator({api:{logger:{warn(){},info(){},error(){}}},config:resolvePluginConfig({autoPoll:false,syncTimeline:false})});
 const client={async callTool(name){
   if(name==='agentbridge_host_task_ensure')return {task:{taskId:'test-task-1234567890'}};
   if(name==='agentbridge_host_coordinator_lease_acquire')return {status:'succeeded',coordinatorLease:{version:1,hostInstanceId:'openclaw-gateway'}};
   if(name==='agentbridge_host_task_observe')return {status:'succeeded'};
   throw Error(name);
 },async callToolResult(){return fixture.result;}};
 const tools=createAgentBridgeProxyTools({context,serverName:'agentbridge',identityRouter:{resolveToolContext:()=>({bound:true,binding:{},client}),endpointKeyForSession:()=> 'workspace:test-user'},trustedResultHandler:(event,ctx)=>coordinator.captureNativeToolResult(event,ctx)});
 const result=await tools.find(t=>t.name===`oa_workflow_history_${fixture.kind}`).execute('test-call',fixture.kind==='query'?{collection:'done',temporal:{kind:'all'}}:{cursor:'test-cursor'});
 assert.deepEqual(result.content,fixture.result.content);
 assert.deepEqual(result.structuredContent,fixture.result.structuredContent);
 const text=result.content[0].text;
 assert.ok(text.length<=12000);
 const payload=JSON.parse(text).result;
 for(const key of ['next_cursor','replay_cursor'])if(payload[key])assert.ok(text.slice(0,12000).includes(payload[key]));
}
"""
        native = subprocess.run([node, "--input-type=module", "-e", script], cwd=root,
            input=json.dumps(fixtures, ensure_ascii=False), text=True, capture_output=True, timeout=30)
        self.assertEqual(native.returncode, 0, native.stderr)

    def test_error_and_login_statuses_are_not_converted_to_success(self):
        cases = [(OaHistoryQueryError("OA_HISTORY_RESULT_TOO_LARGE", "Synthetic 413", status=413), "failed", "OA_HISTORY_RESULT_TOO_LARGE"),
                 (AdapterLoginRequired("Synthetic login expiry"), "requires_user_action", "LOGIN_REQUIRED"),
                 (AdapterSessionCheckUnavailable("Synthetic session check failure"), "requires_user_action", "SESSION_CHECK_UNAVAILABLE")]
        with mcp_fixtures.CentralMcpFixture() as (service, _store, token, client):
            service.invoke.side_effect = self.invoke
            for kind in ("query", "next"):
                for error, status, code in cases:
                    with self.subTest(kind=kind, code=code):
                        self.activate()
                        query = MagicMock()
                        getattr(query, kind).side_effect = error
                        self.service.oa_history_query_service = query
                        arguments = {"collection": "done", "temporal": {"kind": "all"}} if kind == "query" else {"cursor": "synthetic"}
                        response = self.request(client, token, kind, arguments)
                        payload = response["structuredContent"]
                        self.assertEqual(payload["status"], status, response)
                        self.assertEqual(payload["error"]["code"], code)
                        self.assertIsNone(payload["result"])
                        self.assertNotEqual(payload["taskObservation"]["taskStatus"], "succeeded")

    def test_rejected_result_and_trusted_login_card_keep_their_contract(self):
        from agentbridge.mcp.presentation import PRIVATE_INTERACTION_META_KEY
        with mcp_fixtures.CentralMcpFixture() as (service, _store, token, client):
            for kind in ("query", "next"):
                arguments = {"collection": "done", "temporal": {"kind": "all"}} if kind == "query" else {"cursor": "synthetic"}
                rejected = {"status": "rejected", "result": None, "error": {"code": "AUTHORIZATION_CHANGED"}}
                service.invoke.return_value = rejected
                result = self.request(client, token, kind, arguments)
                self.assertEqual(result["structuredContent"], rejected)
                self.assertEqual(json.loads(result["content"][0]["text"]), rejected)
                card = mcp_fixtures._trusted_interaction("http://127.0.0.1:8780/card/synthetic-login")
                card.update(type="credential", title="OA 登录")
                service.invoke.return_value = {"status": "requires_user_action", "result": None,
                    "error": {"code": "LOGIN_REQUIRED"}, "interaction": card}
                result = self.request(client, token, kind, arguments)
                self.assertEqual(result["structuredContent"]["status"], "requires_user_action")
                self.assertEqual(result["_meta"][PRIVATE_INTERACTION_META_KEY], card)
                self.assertIn("trusted user interaction", result["content"][0]["text"])
                self.assertNotIn("/card/synthetic-login", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
