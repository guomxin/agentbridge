from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentbridge.mcp.central import AGENT_FACING_TOOL_SCOPE_REQUIREMENTS
from tools.export_openclaw_agentbridge_catalog import build_catalog


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = (
    REPO_ROOT
    / "integrations"
    / "openclaw-agentbridge"
    / "lib"
    / "agentbridge-tools.json"
)


class OpenClawToolCatalogTests(unittest.TestCase):
    def test_committed_catalog_matches_current_mcp_tools(self) -> None:
        committed = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

        self.assertEqual(committed, build_catalog())
        self.assertFalse(
            any(
                tool["name"].startswith("agentbridge_host_")
                for tool in committed["tools"]
            )
        )
        expected_agent_tools = {
            tool["name"]
            for tool in committed["tools"]
            if tool.get("annotations", {}).get("readOnlyHint") is True
            or tool["name"].endswith("_prepare")
            or tool["name"].endswith("_session_login")
            or tool["name"] in {"agentbridge_task_plan_cancel", "agentbridge_task_cancel", "agentbridge_skill_authoring"}
        }
        self.assertEqual(
            set(AGENT_FACING_TOOL_SCOPE_REQUIREMENTS),
            expected_agent_tools,
        )

        tools_by_name = {tool["name"]: tool for tool in committed["tools"]}
        self.assertTrue(
            {
                "smartlight_runtime_overview",
                "smartlight_rtu_status_list",
                "smartlight_lamp_status_list",
                "smartlight_lamp_alarm_list",
                "smartlight_lamp_alarm_analysis",
                "smartlight_rtu_survey_records",
                "smartlight_energy_record_list",
                "smartlight_energy_analysis",
                "smartlight_lamp_survey_records",
                "smartlight_rtu_leakage_alarm_list",
                "smartlight_rtu_leakage_analysis",
                "smartlight_off_hours_current_list",
                "smartlight_inspection_log_list",
                "smartlight_maintenance_record_list",
            }.issubset(tools_by_name)
        )
        self.assertIn(
            "自然语言“漏电”不得选择本工具",
            tools_by_name["smartlight_leakage_summary"]["description"],
        )
        self.assertIn(
            "设备遥测",
            tools_by_name["smartlight_rtu_survey_records"]["description"],
        )
        self.assertIn(
            "真实 RTU 支路漏电报警",
            tools_by_name["smartlight_rtu_leakage_alarm_list"]["description"],
        )
        self.assertIn(
            "不是人员巡检任务",
            tools_by_name["smartlight_lamp_survey_records"]["description"],
        )
        for tool_name in (
            "smartlight_energy_record_list",
            "smartlight_energy_analysis",
            "smartlight_lamp_survey_records",
            "smartlight_rtu_leakage_alarm_list",
            "smartlight_rtu_leakage_analysis",
            "smartlight_off_hours_current_list",
            "smartlight_inspection_log_list",
            "smartlight_maintenance_record_list",
        ):
            self.assertIn(
                "不得自行扩大范围",
                tools_by_name[tool_name]["description"],
            )
        self.assertIn(
            "必须区分实际查询范围",
            tools_by_name["smartlight_off_hours_current_list"]["description"],
        )
        self.assertIn(
            "currentPolicyWindow",
            tools_by_name["smartlight_off_hours_current_list"]["description"],
        )


if __name__ == "__main__":
    unittest.main()
