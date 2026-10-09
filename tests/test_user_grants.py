import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbridge.core.user_grants import UserGrantConflict, UserGrants


class UserGrantTests(unittest.TestCase):
    def test_unconfigured_user_has_no_business_access_or_template_results(self):
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            with self.assertRaises(PermissionError):
                grants.require_capability("missing", "oa.workflow.pending.list")
            with self.assertRaises(PermissionError):
                grants.require_tool("missing", "oa_workflow_pending_list")
            self.assertNotIn("oa_workflow_pending_list", grants.available_tools("missing"))
            self.assertEqual(grants.filter_templates("missing", {"items": [{"template_id": "anything"}], "raw": "secret"})["items"], [])
            self.assertEqual(grants.effective_scopes("missing"), ["agentbridge:connect"])

    def test_unconfigured_service_denies_execution_and_historical_operation(self):
        from agentbridge.core.central_service import CentralCapabilityService
        from agentbridge.core.document_downloads import DocumentDownloadStore
        with TemporaryDirectory() as temporary:
            service = CentralCapabilityService(home=temporary, base_url="http://oa.test/seeyon")
            with self.assertRaises(PermissionError):
                service.invoke(user_subject="missing", capability_name="oa.workflow.pending.list", arguments={})
            operation, _ = service.operations.create(user_subject="missing", capability_name="oa.workflow.pending.list",
                capability_version="1", input_summary={})
            self.assertEqual(service.list_operations(user_subject="missing")["operations"], [])
            with self.assertRaises(PermissionError):
                service.get_operation(user_subject="missing", operation_id=operation["operation_id"])
            self.assertEqual(service.planning_catalog(user_subject="missing", granted_scopes={"oa:read"})["capabilities"], [])
            downloads = DocumentDownloadStore(service.db_path)
            with self.assertRaises(PermissionError):
                downloads.require_access({"user_subject": "missing", "document_type": "patent_certificate"})

    def test_audit_failure_rolls_back_grant_and_revision(self):
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            def broken_audit(*args):
                raise RuntimeError("audit unavailable")
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                grants.save("user-a", ["oa.workflow.read"], expected_revision=0,
                            actor="admin", reason="test", audit_callback=broken_audit)
            self.assertIsNone(grants.get("user-a"))

    def test_template_filter_uses_exact_contract_and_no_private_tool_discovery(self):
        from agentbridge.adapters.seeyon_leave import LEAVE_TEMPLATE_ID, LEAVE_FORM_APP_ID
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            grants.save("user-a", ["oa.leave.draft"], expected_revision=0, actor="admin", reason="test")
            good = {"template_id": LEAVE_TEMPLATE_ID, "form_app_id": LEAVE_FORM_APP_ID}
            filtered = grants.filter_templates("user-a", {"items": [good, {**good, "form_app_id": "other"}], "raw": "secret"})
            self.assertEqual(filtered["items"], [good])
            self.assertNotIn("raw", filtered)
            tools = grants.available_tools("user-a")
            self.assertIn("oa_template_list", tools)
            self.assertNotIn("oa_leave_save_draft", tools)

    def test_service_execution_and_plan_catalog_follow_current_user_grant(self):
        from agentbridge.core.central_service import CentralCapabilityService

        with TemporaryDirectory() as temporary:
            service = CentralCapabilityService(
                home=temporary, base_url="http://127.0.0.1:8000/seeyon",
            )
            service.user_grants.save(
                "user-a", ["oa.workflow.read"], expected_revision=0,
                actor="admin", reason="workflow only",
            )
            catalog = service.planning_catalog(
                granted_scopes={"oa:read", "oa:read:addressbook"},
                user_subject="user-a",
            )
            names = {item["name"] for item in catalog["capabilities"]}
            self.assertIn("oa.workflow.pending.list", names)
            self.assertNotIn("oa.addressbook.person.search", names)
            with self.assertRaises(PermissionError):
                service.invoke(
                    user_subject="user-a", capability_name="oa.addressbook.person.search",
                    arguments={"query": "Alice"},
                )

    def test_user_permissions_apply_to_all_tokens_and_exports_require_source(self):
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            self.assertIsNone(grants.get("user-a"))
            saved = grants.save(
                "user-a", ["oa.addressbook.read", "oa.addressbook.export"],
                expected_revision=0, actor="admin", reason="test migration",
            )
            self.assertEqual(saved["revision"], 1)
            grants.require_tool("user-a", "oa_addressbook_person_search", {"query": "A"})
            grants.require_tool("user-a", "oa_addressbook_export", {"source": "person_search"})
            with self.assertRaises(PermissionError):
                grants.require_tool("user-a", "oa_addressbook_private_contact_search", {})
            with self.assertRaises(PermissionError):
                grants.require_tool("user-a", "oa_addressbook_export", {"source": "private_contacts"})
            with self.assertRaises(PermissionError):
                grants.require_tool("user-a", "oa_addressbook_group_list", {})
            with self.assertRaises(UserGrantConflict):
                grants.save(
                    "user-a", [], expected_revision=0, actor="admin", reason="stale",
                )
            self.assertEqual(grants.get("user-a")["permissions"], saved["permissions"])

    def test_report_export_requires_matching_read_domain(self):
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            grants.save(
                "user-a", ["smartlight.report.export", "smartlight.energy.read"],
                expected_revision=0, actor="admin", reason="energy reports",
            )
            grants.require_tool("user-a", "smartlight_report_export", {"report_type": "energy_records"})
            with self.assertRaises(PermissionError):
                grants.require_tool("user-a", "smartlight_report_export", {"report_type": "alarm_analysis"})

    def test_certificate_download_requires_read_as_well_as_download(self):
        with TemporaryDirectory() as temporary:
            grants = UserGrants(Path(temporary) / "agentbridge.db")
            grants.save(
                "user-a", ["oa.certificate.download"], expected_revision=0,
                actor="admin", reason="download only",
            )
            with self.assertRaises(PermissionError):
                grants.require_tool("user-a", "oa_certificate_prepare_download", {})
            self.assertNotIn(
                "oa_certificate_prepare_download",
                grants.available_tools("user-a"),
            )
