import copy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.business_skills import SkillRegistry, SkillRejected, SkillStore, skill_catalog, dependency_state, validate_binding, skill_bundle
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.user_grants import UserGrantConflict


class BusinessSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = CentralCapabilityService(home=self.temp.name, base_url="http://oa.test/seeyon")
        self.store = self.service.skills
        self.service.user_grants.save("alice", ["oa.workflow.read", "taihua.work_log.create"], expected_revision=0, actor="admin", reason="fixture")

    def assign(self, value=None, revision=0, user="alice"):
        return self.store.save("user:" + user, value if value is not None else {"oa-work-log": {"profiles": ["preview", "fill"]}}, expected_revision=revision, actor="admin", reason="fixture")

    def test_unassigned_and_other_users_cannot_load(self):
        self.assertEqual(skill_catalog(self.service, "alice")["items"], [])
        with self.assertRaises(SkillRejected):
            self.store.bind("alice", "oa-work-log", "preview")
        self.assign()
        binding = self.store.bind("alice", "oa-work-log", "preview")
        with self.assertRaises(SkillRejected):
            self.store.binding("bob", binding)
        self.assertEqual(skill_catalog(self.service, "bob")["items"], [])

    def test_dependency_is_not_a_grant_and_preview_is_independent(self):
        self.service.user_grants.save("alice", ["oa.workflow.read"], expected_revision=1, actor="admin", reason="read only")
        self.assign()
        item = skill_catalog(self.service, "alice")["items"][0]
        self.assertTrue(item["profiles"]["preview"]["available"])
        self.assertFalse(item["profiles"]["fill"]["available"])
        binding = self.store.binding("alice", self.store.bind("alice", "oa-work-log", "preview"))
        with self.assertRaisesRegex(SkillRejected, "预览"):
            validate_binding(self.service, "alice", binding, capability="taihua.work_log.create.prepare")

    def test_task_retains_revocation_even_after_reassignment_and_restart(self):
        self.assign()
        binding = self.store.bind("alice", "oa-work-log", "preview")
        self.store.attach("alice", "task-a", binding)
        self.assign({}, 1)
        self.assign(revision=2)
        restarted = SkillStore(self.store.db_path)
        with self.assertRaisesRegex(SkillRejected, "撤销"):
            restarted.for_task("alice", "task-a")
        self.assertIsNone(restarted.for_task("bob", "task-a"))

    def test_global_disable_and_profile_reduction_stop_bindings(self):
        self.assign()
        binding = self.store.bind("alice", "oa-work-log", "fill")
        self.assign({"oa-work-log": {"profiles": ["preview"]}}, 1)
        with self.assertRaises(SkillRejected): self.store.binding("alice", binding)
        preview = self.store.bind("alice", "oa-work-log", "preview")
        self.store.save("global", {"oa-work-log": "disabled"}, expected_revision=0, actor="admin", reason="stop")
        with self.assertRaises(SkillRejected): self.store.binding("alice", preview)

    def test_conflict_and_audit_failure_are_atomic(self):
        self.assign()
        with self.assertRaises(UserGrantConflict): self.assign({}, 0)
        def fail(*args): raise RuntimeError("audit unavailable")
        with self.assertRaises(RuntimeError):
            self.store.save("user:alice", {}, expected_revision=1, actor="admin", reason="test", audit_callback=fail)
        self.assertIn("oa-work-log", self.store.config("user:alice")["value"])
        self.assertEqual(self.store.config("user:alice")["revision"], 1)

    def test_versions_are_immutable_and_old_snapshots_survive_upgrade(self):
        self.assign()
        binding = self.store.bind("alice", "oa-work-log", "preview")
        registry = SkillRegistry()
        registry.items = copy.deepcopy(registry.items)
        original_version = registry.items["oa-work-log"]["manifest"]["version"]
        registry.items["oa-work-log"]["content_hash"] = "changed"
        with self.assertRaisesRegex(ValueError, "increment version"):
            SkillStore(self.store.db_path, registry)
        major, minor, patch = map(int, original_version.split("."))
        registry.items["oa-work-log"]["manifest"]["version"] = f"{major}.{minor}.{patch + 1}"
        newer = SkillStore(self.store.db_path, registry)
        self.assertEqual(newer.binding("alice", binding)["version"], original_version)
        with self.assertRaises(SkillRejected): newer.bind("alice", "oa-work-log", "preview", expected_version=original_version)

    def test_packaged_resources_reject_path_escape(self):
        root = Path(self.temp.name) / "packages"
        shutil.copytree(Path("agentbridge/business_skills"), root)
        path = root / "log-review/manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["resources"] = ["../../outside.md"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "path"):
            SkillRegistry(root)

    def test_database_dependency_stays_within_one_source_no_free_sql_needed(self):
        manifest = self.store.registry.get("log-review")["manifest"]
        catalog = {"sources": [{"source_id": "a", "capabilities": [{"name": "database.directory"}]},
                               {"source_id": "b", "capabilities": [{"name": "database.logs.query"}]}]}
        with patch("agentbridge.database.independent.IndependentDatabase.catalog", return_value=catalog):
            self.assertFalse(dependency_state(self.service, "alice", manifest, "review", "a")["available"])
            catalog["sources"][0]["capabilities"].append({"name": "database.logs.query"})
            self.assertTrue(dependency_state(self.service, "alice", manifest, "review", "a")["available"])
            self.assertTrue(dependency_state(self.service, "alice", manifest, "review")["needs_source"])
            self.assertFalse(dependency_state(self.service, "alice", manifest, "review", "b")["available"])

    def test_task_binding_cannot_be_replaced(self):
        self.assign()
        one = self.store.bind("alice", "oa-work-log", "preview")
        two = self.store.bind("alice", "oa-work-log", "fill")
        self.store.attach("alice", "task-a", one)
        with self.assertRaises(SkillRejected): self.store.attach("alice", "task-a", two)

    def test_revocation_is_checked_inside_authorization_transaction(self):
        from contextlib import closing
        self.assign()
        binding = self.store.bind("alice", "oa-work-log", "fill")
        self.store.attach("alice", "task-a", binding)
        authorization = self.service.write_authorizations.create(user_subject="alice", system_id="taihua", session_id="session-a",
            capability_name="taihua.work_log.create", capability_version="1", prepare_operation_id="prepare-a",
            plan={"content":"fixture"}, summary={"title":"fixture"}, card_base_url="http://127.0.0.1:8780")
        csrf = self.service.write_authorizations.issue_csrf(authorization["authorization_id"])
        self.service.write_authorizations.decide(authorization["authorization_id"], decision="approve", csrf_token=csrf, csrf_cookie=csrf)
        interaction = self.service.interactions.register(interaction_type="execution_authorization", user_subject="alice", system_id="taihua",
            session_id="session-a", resource_id=authorization["authorization_id"], title="fixture", message="fixture", display={},
            resume_spec={"kind":"capability"}, created_at=authorization["created_at"], expires_at=authorization["expires_at"])
        with closing(self.store.connect()) as db, db:
            db.execute("INSERT INTO task_interactions (task_id,interaction_id,user_subject,linked_at) VALUES (?,?,?,?)", ("task-a", interaction["interaction_id"], "alice", authorization["created_at"]))
        self.assign({}, 1)
        with self.assertRaises(SkillRejected):
            self.service.write_authorizations.consume(authorization["authorization_id"], user_subject="alice", system_id="taihua", session_id="session-a",
                capability_name="taihua.work_log.create", capability_version="1", commit_operation_id="commit-a",
                before_consume=lambda db: self.service.guard_skill_authorization(db, authorization["authorization_id"], "alice"))
        self.assertEqual(self.service.write_authorizations.get(authorization["authorization_id"])["state"], "approved")

    def test_invalid_configuration_and_arbitrary_prompt_rejected(self):
        for value in [{"unknown": {}}, {"oa-work-log": {"prompt": "override"}},
                      {"oa-work-log": {"profiles": ["commit"]}}, {"oa-work-log": {"source_id": "../secret"}}]:
            with self.subTest(value=value), self.assertRaises(ValueError): self.assign(value)

    def test_catalog_is_not_content_and_contains_five_packages(self):
        result = skill_catalog(self.service, "alice", include_all=True)
        self.assertEqual(len(result["items"]), 5)
        self.assertNotIn("resources", result["items"][0])
        self.assertTrue(all("SKILL.md" in item["resources"] for item in self.store.registry.items.values()))
        self.assertTrue(all(set(item["selection"]) == {"use_when", "not_for", "output"} for item in result["items"]))

    def test_bundle_is_profile_scoped_and_historical_snapshot_is_pinned(self):
        item = copy.deepcopy(self.store.registry.get("log-review"))
        binding = {"snapshot": item, "profile": "review"}
        bundled = skill_bundle(binding)
        self.assertEqual(bundled["loaded_resources"], ["SKILL.md", "references/evidence.md"])
        self.assertIn(item["resources"]["references/evidence.md"], bundled["content"])
        item["resources"]["optional.md"] = "not required"
        self.assertNotIn("not required", skill_bundle(binding)["content"])
        item["manifest"]["required_resources"]["preview"] = []
        self.assertEqual(skill_bundle({**binding, "profile": "preview"})["loaded_resources"], ["SKILL.md"])
        del item["manifest"]["required_resources"]
        self.assertEqual(skill_bundle(binding)["loaded_resources"], bundled["loaded_resources"])
        del item["resources"]["references/evidence.md"]
        with self.assertRaisesRegex(SkillRejected, "必读资料"):
            skill_bundle(binding)

    def test_invalid_required_resources_and_selection_fail_at_registry_load(self):
        root = Path(self.temp.name) / "packages"
        shutil.copytree(Path("agentbridge/business_skills"), root)
        path = root / "log-review/manifest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for required in ({}, {"review": ["missing.md"]}, {"review": ["SKILL.md"]},
                         {"review": ["references/evidence.md"] * 2}, {"review": "references/evidence.md"}):
            with self.subTest(required=required):
                path.write_text(json.dumps({**original, "required_resources": required}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "required resource"):
                    SkillRegistry(root)
        path.write_text(json.dumps({**original, "selection": {"use_when": "anything"}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "selection"):
            SkillRegistry(root)
        path.write_text(json.dumps(original), encoding="utf-8")
        (root / "log-review/references/evidence.md").unlink()
        with self.assertRaises(FileNotFoundError):
            SkillRegistry(root)

    def test_load_cards_are_deduplicated_and_scoped_to_user(self):
        result = {"status": "succeeded", "binding_id": "binding", "version": "1.0.0"}
        for resource in ("SKILL.md", "SKILL.md", "references/evidence.md"):
            self.store.record_load("alice", "log-review", "review", resource, result)
        self.store.record_load("bob", "oa-work-log", "fill", "SKILL.md",
            {"status": "rejected", "error": {"code": "SKILL_UNAVAILABLE", "message": "未分配"}})
        self.assertEqual(len(self.store.load_history("alice")), 1)
        self.assertEqual(self.store.load_history("alice")[0]["name"], "日志总结与事项复核")
        self.assertEqual(self.store.load_history("bob")[0]["status"], "rejected")
        self.assertEqual(self.store.load_history("unknown"), [])

    def test_real_mcp_host_load_revoke_and_cross_user_binding(self):
        from starlette.testclient import TestClient
        from agentbridge.core.mcp_identities import McpIdentityTokenStore
        from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
        store = McpIdentityTokenStore(self.service.db_path)
        tokens = {u: store.issue(user_subject=u, expected_principal_ref=u, ttl_seconds=3600) for u in ("alice", "bob")}
        profile = json.loads(Path("schemas/agent-host/v1/test-vectors.json").read_text(encoding="utf-8"))["profiles"][0]["value"]
        meta = {"io.agentbridge/host-context": {"version": "1", "agentHost": "reference-host", "hostInstanceId": profile["hostInstanceId"], "hostVersion": "0.1.0"}}
        for user, token in tokens.items():
            self.service.negotiate_host(user_subject=user, token_id=token["token_id"], profile=profile)
        config = validate_central_mcp_server_config(host="127.0.0.1", port=8790, public_base_url="http://testserver", tls_cert=None, tls_key=None)
        server = create_central_mcp_server(service=self.service, identity_store=store, config=config, auth_card_base_url="http://127.0.0.1:8780")
        with TestClient(server.streamable_http_app()) as client:
            def call(name, args=None, user="alice", binding=None, task_id=None):
                headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + tokens[user]["token"], "MCP-Protocol-Version": "2025-06-18"}
                context = {**meta, **({"agentbridge/skill": {"bindingId": binding}} if binding else {})}
                if task_id:
                    from agentbridge.mcp.central import TASK_CONTEXT_META_KEY
                    context[TASK_CONTEXT_META_KEY] = {'taskId': task_id}
                return client.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":name, "method":"tools/call", "params":{"name":name,"arguments":args or {},"_meta":context}}).json()["result"]
            self.assertEqual(call("agentbridge_skill_catalog")["structuredContent"]["items"], [])
            self.assign()
            result = call("agentbridge_skill_get", {"skill_id":"oa-work-log", "profile":"preview", "expected_version":self.store.registry.get("oa-work-log")["manifest"]["version"]})
            self.assertFalse(result.get("isError"), result)
            binding = result["structuredContent"]["binding_id"]
            execution = result['structuredContent']['execution']
            self.assertEqual(execution['effects'], ['read'])
            self.assertFalse(execution['legacy'])
            self.assertFalse(execution['allow_batch'])
            self.assertNotIn('taihua.work_log.create.prepare', execution['capabilities'])
            self.assertEqual(result["structuredContent"]["loaded_resources"], ["SKILL.md"])
            self.assertNotIn("resource", result["structuredContent"])
            mismatch = call("agentbridge_skill_get", {"skill_id":"oa-work-log", "profile":"preview", "expected_version":"0.0.1"}, binding=binding)
            self.assertEqual(mismatch["structuredContent"]["error"]["code"], "SKILL_VERSION_CHANGED")
            self.assertEqual(self.store.load_history("alice")[0]["status"], "rejected")
            # Real host load includes references without allowing the host to select a file.
            self.assign({"oa-work-log": {"profiles": ["preview", "fill"]}, "log-review": {"profiles": ["review"]}}, 1)
            with patch("agentbridge.database.independent.IndependentDatabase.catalog", return_value={"sources": [
                {"source_id":"a", "capabilities":[{"name":"database.directory"}, {"name":"database.logs.query"}]}]}):
                loaded = call("agentbridge_skill_get", {"skill_id":"log-review", "profile":"review", "source_id":"a"})["structuredContent"]
                self.assertEqual(loaded["loaded_resources"], ["SKILL.md", "references/evidence.md"])
                self.assertIn("结论核对", loaded["content"])
                with patch('agentbridge.database.independent.IndependentDatabase.execute', return_value={'status': 'succeeded'}) as execute:
                    allowed_db = call('database_execute', {'capability': 'database.directory', 'arguments': {'entity': 'users'}, 'source_id': 'a'}, binding=loaded['binding_id'])
                    self.assertFalse(allowed_db.get('isError'), allowed_db)
                    self.assertEqual(execute.call_count, 1)
                    optional_db = call('database_execute', {'capability': 'database.free.read', 'arguments': {'sql': 'SELECT 1'}, 'source_id': 'a'}, binding=loaded['binding_id'])
                    self.assertFalse(optional_db.get('isError'), optional_db)
                    self.assertEqual(execute.call_count, 2)
                    denied_other = call('database_execute', {'capability': 'database.directory', 'arguments': {'entity': 'users'}, 'source_id': 'a'}, binding=binding)
                    self.assertTrue(denied_other.get('isError'), denied_other)
                    self.assertEqual(execute.call_count, 2)
                    task_id = 'skill-source-task-0001'
                    self.store.attach('alice', task_id, loaded['binding_id'])
                    denied_source = call('database_execute', {'capability':'database.directory',
                        'arguments':{'entity':'users'}, 'source_id':'b'}, task_id=task_id)
                    self.assertTrue(denied_source.get('isError'), denied_source)
                    self.assertIn('数据源与本次业务助手选择不一致', str(denied_source))
                    self.assertEqual(execute.call_count, 2)
                    allowed_task = call('database_execute', {'capability':'database.directory',
                        'arguments':{'entity':'users'}, 'source_id':'a'}, task_id=task_id)
                    self.assertFalse(allowed_task.get('isError'), allowed_task)
                    self.assertEqual(execute.call_count, 3)
            self.assertEqual(self.store.load_history("alice")[0]["loaded_resources"], loaded["loaded_resources"])
            self.assertEqual(self.store.load_history("alice")[0]["status"], "succeeded")
            denied = call("agentbridge_skill_get", {"skill_id":"oa-work-log", "profile":"preview"}, user="bob", binding=binding)
            self.assertEqual(denied["structuredContent"]["error"]["code"], "SKILL_BINDING_INVALID")
            with patch.object(self.service, "invoke", return_value={"status":"succeeded"}) as invoke:
                allowed = call("oa_workflow_pending_list", binding=binding)
                self.assertFalse(allowed.get("isError"), allowed)
                invoke.assert_called_once()
                self.assign({}, 2)
                blocked = call("oa_workflow_pending_list", binding=binding)
                self.assertTrue(blocked["isError"])
                self.assertEqual(invoke.call_count, 1)
            # An approved pure-text helper cannot reach database execution even
            # when an authenticated host supplies a syntactically valid call.
            authoring = self.service.skill_authoring
            draft = authoring.save('alice', proposal={'name': '纯文字', 'description': '整理给定文字',
                'selection': {'use_when': '整理文字', 'not_for': '查询业务', 'output': '文字'},
                'instructions': '只整理输入文字。'}, request_key='pure-draft')
            sample = authoring.test_start('alice', draft_id=draft['draft_id'], expected_revision=1,
                profile='use', prompt='合成输入', request_key='pure-test')
            authoring.test_result('alice', test_id=sample['test_id'], output='合成输出')
            request = authoring.submit('alice', draft_id=draft['draft_id'], expected_revision=1,
                request_key='pure-submit', reason='私有文字方法')
            authoring.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'],
                decision='approve', reason='合成核对', reviewed_tests=True, manual_quality_reason='逐句核对输入输出')
            pure = call('agentbridge_skill_get', {'skill_id': draft['skill_id'], 'profile': 'use'})['structuredContent']
            self.assertEqual(pure['execution']['capabilities'], [])
            with patch('agentbridge.database.independent.IndependentDatabase.execute') as execute:
                denied = call('database_execute', {'capability': 'database.directory', 'arguments': {'entity': 'users'}}, binding=pure['binding_id'])
                self.assertTrue(denied.get('isError'), denied)
                execute.assert_not_called()
            restricted = authoring.save('alice', proposal={'name': '受限导出', 'description': '仅导出日志查询',
                'selection': {'use_when': '日志导出', 'not_for': '自由查询', 'output': 'CSV'}, 'instructions': '只使用日志查询。',
                'profiles': {'use': {'database': {'all': ['database.logs.query']}, 'execution': {
                    'effects': ['read'], 'allow_batch': False, 'capabilities': ['database.logs.query', 'database.report.export']}}}},
                request_key='restricted-draft')
            sample = authoring.test_start('alice', draft_id=restricted['draft_id'], expected_revision=1,
                profile='use', prompt='合成输入', request_key='restricted-test')
            authoring.test_result('alice', test_id=sample['test_id'], output='合成输出')
            request = authoring.submit('alice', draft_id=restricted['draft_id'], expected_revision=1,
                request_key='restricted-submit', reason='受限日志导出')
            authoring.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'],
                decision='approve', reason='合成核对', reviewed_tests=True, manual_quality_reason='逐句核对输入输出')
            with patch('agentbridge.database.independent.IndependentDatabase.catalog', return_value={'sources': [
                {'source_id': 'a', 'capabilities': [{'name': 'database.logs.query'}]}]}):
                restricted_loaded = call('agentbridge_skill_get', {'skill_id': restricted['skill_id'], 'profile': 'use', 'source_id': 'a'})['structuredContent']
                with patch('agentbridge.database.independent.IndependentDatabase.execute') as execute:
                    denied = call('database_execute', {'capability': 'database.report.export', 'source_id': 'a',
                        'arguments': {'query_capability': 'database.free.read', 'query_arguments': {'sql': 'SELECT 1'}, 'request_key': 'export'}},
                        binding=restricted_loaded['binding_id'])
                    self.assertTrue(denied.get('isError'), denied)
                    execute.assert_not_called()


if __name__ == "__main__": unittest.main()
