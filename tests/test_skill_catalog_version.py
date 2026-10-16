from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.business_skills import skill_catalog
from agentbridge.core.skill_catalog_version import skill_catalog_stamps
from tests.authorization_fixtures import authorized_service


class SkillCatalogVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = authorized_service(home=self.temp.name, base_url="http://oa.test/seeyon")
        self.service.skills.save("user:user-a", {"oa-work-log": {"profiles": ["preview", "fill"]}}, expected_revision=0, actor="test", reason="fixture")

    def test_stamp_is_stable_and_shared_by_prebuilt_catalog_and_runtime(self):
        catalog = skill_catalog(self.service, "user-a")
        stamps = skill_catalog_stamps(self.service, "user-a", catalog=catalog)
        self.assertEqual(stamps, self.service.skill_catalog_stamps("user-a"))
        self.assertTrue(all(value.startswith("sha256:") and len(value) == 71 for value in stamps.values()))

    def test_assignment_global_status_content_and_dependency_changes_invalidate_catalog(self):
        initial = skill_catalog(self.service, "user-a")
        original = skill_catalog_stamps(self.service, "user-a", catalog=initial)
        for field, changed in (("content_hash", "different-content"), ("status", "disabled"),
                               ("settings", {"profiles": ["preview"]}), ("profiles", {"preview": {"available": False}})):
            modified = deepcopy(initial)
            modified["items"][0][field] = changed
            self.assertNotEqual(original["catalogVersion"], skill_catalog_stamps(self.service, "user-a", catalog=modified)["catalogVersion"])
        self.service.skills.save("global", {"oa-work-log": "disabled"}, expected_revision=0, actor="test", reason="fixture")
        self.assertNotEqual(original["catalogVersion"], self.service.skill_catalog_stamps("user-a")["catalogVersion"])

    def test_permission_revision_and_actual_tool_access_are_part_of_validator(self):
        before = self.service.skill_catalog_stamps("user-a")
        grant = self.service.user_grants.get("user-a")
        self.service.user_grants.save("user-a", ["oa.workflow.read"], expected_revision=grant["revision"], actor="test", reason="fixture")
        after = self.service.skill_catalog_stamps("user-a")
        self.assertNotEqual(before["permissionVersion"], after["permissionVersion"])
        self.assertNotEqual(before["catalogVersion"], after["catalogVersion"])


if __name__ == "__main__":
    unittest.main()
