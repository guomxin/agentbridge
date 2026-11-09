from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from starlette.testclient import TestClient

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.planning_catalog import (
    PLANNING_DISCOVERY_MAX_CHARS,
    discover_planning_catalog,
    planning_catalog_response_chars,
)
from agentbridge.core.skill_catalog import public_capability_catalog
from agentbridge.core.user_grants import PERMISSIONS
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config


class PlanningCatalogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="planning-catalog-discovery-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.service = CentralCapabilityService(home=self.home, base_url="http://oa.test/seeyon")
        self.subject = "catalog-user"
        self.service.user_grants.save(self.subject, list(PERMISSIONS), expected_revision=0,
                                     actor="test", reason="isolated catalog fixture")
        self.scopes = self.service.user_grants.effective_scopes(self.subject)

    def catalog(self, **arguments):
        return self.service.planning_catalog(granted_scopes=self.scopes, user_subject=self.subject,
                                             **arguments)

    def assert_bounded(self, response):
        self.assertLessEqual(planning_catalog_response_chars(response), PLANNING_DISCOVERY_MAX_CHARS)
        self.assertEqual(json.loads(json.dumps(response, ensure_ascii=False)), response)

    def test_all_summary_pages_are_bounded_and_cover_each_authorized_entry_once(self):
        full = self.catalog()
        expected = {("capability", entry["name"]) for entry in full["capabilities"]}
        expected.update(("transform", entry["name"]) for entry in full["transforms"])
        seen = []
        cursor = None
        revision = None
        page_count = 0
        while True:
            page = self.catalog(view="summary", cursor=cursor)
            self.assert_bounded(page)
            self.assertEqual(page["startOffset"], len(seen))
            self.assertEqual(page["returnedCount"], len(page["entries"]))
            self.assertEqual(page["total"], len(expected))
            self.assertEqual(page["catalogRevision"], revision or page["catalogRevision"])
            revision = page["catalogRevision"]
            for entry in page["entries"]:
                self.assertNotIn("inputSchema", entry)
                self.assertNotIn("outputSchema", entry)
                seen.append((entry["kind"], entry["name"]))
            page_count += 1
            if not page["hasMore"]:
                self.assertIsNone(page["nextCursor"])
                break
            self.assertTrue(page["entries"])
            self.assertNotEqual(page["nextCursor"], cursor)
            cursor = page["nextCursor"]
            self.assertLess(page_count, 30)
        self.assertGreater(page_count, 1)
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(seen), len(expected))

    def test_named_and_sectioned_contracts_preserve_exact_schemas_and_revision(self):
        full = self.catalog()
        entries = {("capability", item["name"]): item for item in full["capabilities"]}
        entries.update({("transform", item["name"]): item for item in full["transforms"]})
        revision = self.catalog(view="summary")["catalogRevision"]
        for kind, name in (("capability", "oa.workflow.done.list"),
                           ("transform", "merge_work_items.v1"),
                           ("transform", "work_items_to_log_draft.v2")):
            with self.subTest(name=name):
                original = entries[kind, name]
                contract_revisions = set()
                reconstructed = {}
                for section in ("input", "output", "planning"):
                    detail = self.catalog(view="detail", kind=kind, name=name, section=section,
                                          expected_revision=revision)
                    self.assert_bounded(detail)
                    self.assertEqual(detail["completeSections"], [section])
                    contract_revisions.add(detail["contractRevision"])
                    reconstructed.update(detail["contract"])
                self.assertEqual(reconstructed, original)
                self.assertEqual(len(contract_revisions), 1)
                detail = self.catalog(view="detail", kind=kind, name=name, expected_revision=revision)
                self.assert_bounded(detail)
                if "error" in detail:
                    self.assertEqual(detail["error"]["code"], "PLANNING_CATALOG_SECTION_REQUIRED")
                    self.assertEqual(detail["nextAction"]["arguments"]["expected_revision"], revision)
                else:
                    self.assertEqual(detail["contract"], original)
                    self.assertEqual(detail["completeSections"], ["input", "output", "planning"])

    def test_cursor_is_bound_to_filters_and_authorized_catalog_revision(self):
        first = self.catalog(view="summary")
        self.assertTrue(first["hasMore"])
        changed_filter = self.catalog(view="summary", kind="capability", cursor=first["nextCursor"])
        self.assertEqual(changed_filter["error"]["code"], "PLANNING_CATALOG_INVALID_QUERY")
        changed = deepcopy(self.catalog())
        changed["capabilities"][0]["description"] += " changed"
        next_page = discover_planning_catalog(changed, cursor=first["nextCursor"])
        self.assertEqual(next_page["error"]["code"], "PLANNING_CATALOG_CHANGED")
        detail = discover_planning_catalog(changed, view="detail", kind="capability",
                                          name="oa.workflow.done.list", section="input",
                                          expected_revision=first["catalogRevision"])
        self.assertEqual(detail["error"]["code"], "PLANNING_CATALOG_CHANGED")

    def test_revoked_permission_invalidates_cursor_and_denies_named_contract(self):
        first = self.catalog(view="summary")
        self.service.user_grants.save(self.subject, [], expected_revision=1,
                                     actor="test", reason="revoke during discovery")
        stale = self.catalog(view="summary", cursor=first["nextCursor"])
        self.assertEqual(stale["error"]["code"], "PLANNING_CATALOG_CHANGED")
        hidden = self.catalog(view="detail", kind="capability", name="oa.workflow.done.list")
        unknown = self.catalog(view="detail", kind="capability", name="oa.missing.capability")
        self.assertEqual(hidden, unknown)
        self.assertNotIn("oa.workflow.done.list", str(hidden))
        summary = self.catalog(view="summary", kind="capability")
        self.assertEqual(summary["entries"], [])
        self.assertEqual(self.catalog()["prepareInputGuide"]["examples"], [])

    def test_scope_and_user_grant_filter_examples_and_exclude_hidden_commits(self):
        self.service.user_grants.save(self.subject, ["oa.workflow.read"], expected_revision=1,
                                     actor="test", reason="workflow only")
        full = self.catalog()
        names = {entry["name"] for entry in full["capabilities"]}
        self.assertIn("oa.workflow.done.list", names)
        self.assertNotIn("oa.addressbook.person.search", names)
        for example in full["prepareInputGuide"]["examples"]:
            for step in example["steps"]:
                if step["kind"] == "capability":
                    self.assertIn(step["capabilityName"], names)
        self.assertTrue(full["prepareInputGuide"]["examples"])
        self.assertNotIn("taihua.work_log.create.prepare", str(full["prepareInputGuide"]["examples"]))
        for name in ("taihua.work_log.create", "oa.business_settlement.approve"):
            detail = self.catalog(view="detail", kind="capability", name=name)
            self.assertEqual(detail["error"]["code"], "PLANNING_CATALOG_ENTRY_UNAVAILABLE")
        scoped = self.service.planning_catalog(user_subject=self.subject, granted_scopes=set(), view="summary")
        self.assertFalse(any(entry["kind"] == "capability" for entry in scoped["entries"]))

    def test_system_filter_preserves_common_transforms_and_summary_roles(self):
        page = self.catalog(view="summary", system="oa", kind="capability")
        self.assertTrue(page["entries"])
        self.assertTrue(all(item["system"] == "oa" for item in page["entries"]))
        transforms = self.catalog(view="summary", system="oa", kind="transform")
        merge = next(item for item in transforms["entries"] if item["name"] == "merge_work_items.v1")
        self.assertTrue(merge["resultProjection"])
        self.assertTrue(merge["requiredBoundInputs"])
        self.assertFalse(transforms["hasMore"])

    def test_invalid_combinations_and_cursors_return_bounded_actionable_errors(self):
        for arguments in ({"view": "missing"}, {"view": "summary", "name": "oa.workflow.done.list"},
                          {"view": "summary", "section": "input"}, {"view": "detail"},
                          {"view": "detail", "kind": "capability", "name": "oa.workflow.done.list", "system": "oa"},
                          {"view": "summary", "cursor": "!invalid"},
                          {"view": "summary", "cursor": "W10"},
                          {"view": "summary", "cursor": "e30"}):
            with self.subTest(arguments=arguments):
                response = self.catalog(**arguments)
                self.assert_bounded(response)
                self.assertEqual(response["error"]["code"], "PLANNING_CATALOG_INVALID_QUERY")

    def test_unicode_budget_never_truncates_a_contract_or_mutates_internal_catalog(self):
        catalog = self.catalog()
        for entry in catalog["capabilities"]:
            entry["description"] = "😀" * 160
        original = deepcopy(catalog)
        page = discover_planning_catalog(catalog)
        self.assert_bounded(page)
        self.assertTrue(page["hasMore"])
        self.assertEqual(page["entries"][0]["description"], "😀" * 160)
        self.assertEqual(catalog, original)
        item = next(entry for entry in catalog["capabilities"] if entry["name"] == "oa.workflow.done.list")
        item["inputSchema"] = {"type": "object", "description": "😀" * 12000}
        oversized = discover_planning_catalog(catalog, view="detail", kind="capability",
                                               name=item["name"], section="input")
        self.assert_bounded(oversized)
        self.assertEqual(oversized["error"]["code"], "PLANNING_CATALOG_SECTION_TOO_LARGE")
        self.assertNotIn("contract", oversized)

    def test_internal_full_catalog_and_skill_authoring_keep_complete_contracts(self):
        full = self.catalog()
        self.assertEqual(full["schemaVersion"], "agentbridge.planning-catalog.v2")
        self.assertIn("stepSchema", full["prepareInputGuide"])
        before = deepcopy(full)
        self.catalog(view="summary")
        self.assertEqual(self.catalog(), before)
        public = public_capability_catalog(self.service)
        item = next(entry for entry in public if entry["name"] == "oa.workflow.done.list")
        self.assertEqual(item["inputSchema"], self.service.registry.get(item["name"]).input_schema)
        self.assertEqual(item["outputSchema"], self.service.registry.get(item["name"]).output_schema)

    def test_mcp_default_and_detail_wire_responses_are_complete_and_bounded(self):
        store = McpIdentityTokenStore(self.home / "agentbridge.db")
        issued = store.issue(user_subject=self.subject, expected_principal_ref="Catalog Fixture", ttl_seconds=600)
        config = validate_central_mcp_server_config(host="127.0.0.1", port=8790,
            public_base_url="http://testserver", tls_cert=None, tls_key=None)
        server = create_central_mcp_server(service=self.service, identity_store=store, config=config,
                                           auth_card_base_url="http://127.0.0.1:8780")
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
                   "Authorization": "Bearer " + issued["token"]}
        with TestClient(server.streamable_http_app()) as client:
            listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
            tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "agentbridge_task_plan_catalog")
            self.assertEqual(tool["inputSchema"]["properties"]["view"]["default"], "summary")
            self.assertNotIn("full", tool["inputSchema"]["properties"]["view"]["enum"])
            for index, arguments in enumerate(({}, {"view": "detail", "kind": "capability", "name": "oa.workflow.done.list"},
                                                {"view": "detail", "kind": "transform", "name": "work_items_to_log_draft.v2", "section": "input"})):
                response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": index,
                    "method": "tools/call", "params": {"name": "agentbridge_task_plan_catalog", "arguments": arguments}})
                self.assertEqual(response.status_code, 200)
                result = response.json()["result"]
                self.assertFalse(result.get("isError"))
                self.assert_bounded(result["structuredContent"])
                text = "".join(item["text"] for item in result["content"] if item["type"] == "text")
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, PLANNING_DISCOVERY_MAX_CHARS)
                self.assertEqual(json.loads(text), result["structuredContent"])
                self.assertNotIn("error", result["structuredContent"])


if __name__ == "__main__":
    unittest.main()
