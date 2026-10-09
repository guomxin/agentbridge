from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class DocumentationTests(unittest.TestCase):
    def test_generated_facts_match_all_system_totals_and_plugin_source(self):
        from scripts.current_facts import code_facts, render

        facts = code_facts()
        self.assertEqual(sum(sum(counts.values()) for counts in facts['systems'].values()), facts['capabilityTotal'])
        text = (ROOT / 'docs/项目当前状态.md').read_text(encoding='utf-8')
        self.assertIn(render(facts), text)
        for field in ('deployment', 'automaticValidation', 'businessAcceptance'):
            self.assertEqual(facts[field], 'not_asserted')

    def test_current_inventory_matches_constructed_capability_and_mcp_registries(self):
        from agentbridge.core.central_service import CentralCapabilityService
        from agentbridge.core.mcp_identities import McpIdentityTokenStore
        from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config

        text = (ROOT / "docs" / "项目当前状态.md").read_text(encoding="utf-8")
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(home=Path(tmp), base_url="http://oa.example.test/seeyon/main.do?method=main")
            config = validate_central_mcp_server_config(host="127.0.0.1", port=8790,
                public_base_url="http://testserver", tls_cert=None, tls_key=None)
            server = create_central_mcp_server(service=service, identity_store=McpIdentityTokenStore(service.db_path),
                config=config, auth_card_base_url="http://127.0.0.1:8780")
            self.assertEqual(re.findall(r"中央能力注册表共 (\d+) 个业务能力", text), [str(len(service.registry.list()))])
            self.assertEqual(re.findall(r"本期中央目录共有 (\d+) 个 MCP 工具", text), [str(len(server._tool_manager.list_tools()))])
            self.assertNotRegex(text, r"MCP 源码目录共有 \d+ 个工具")

    def test_relative_markdown_links_resolve(self):
        paths = [
            *ROOT.glob("*.md"),
            *(ROOT / "docs").rglob("*.md"),
            ROOT / "integrations" / "openclaw-agentbridge" / "README.md",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for raw_target in MARKDOWN_LINK.findall(text):
                target = raw_target.strip().strip("<>")
                parsed = urlparse(target)
                if parsed.scheme or target.startswith("#"):
                    continue
                relative = unquote(target.split("#", 1)[0])
                if not relative:
                    continue
                resolved = (path.parent / relative).resolve()
                with self.subTest(document=path.relative_to(ROOT), target=target):
                    self.assertTrue(resolved.exists(), f"missing documentation link: {resolved}")

    def test_current_documentation_has_one_entry_point(self):
        index = ROOT / "docs" / "文档导航.md"
        text = index.read_text(encoding="utf-8")
        for expected in (
            "./项目当前状态.md",
            "./架构设计/受控写入模型.md",
            "./部署运维/当前内网部署.md",
            "./部署运维/开发验证与发布流程.md",
            "./系统适配/系统适配导航.md",
            "./验收记录/验收记录导航.md",
            "./后续规划/后续增强事项.md",
            "./历史归档/历史资料导航.md",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_current_documentation_uses_category_directories(self):
        self.assertEqual(
            {path.name for path in ROOT.glob("*.md")},
            {"README.md"},
        )
        self.assertEqual(
            {path.name for path in (ROOT / "docs").glob("*.md")},
            {"文档导航.md", "项目当前状态.md"},
        )
        for category in (
            "架构设计",
            "平台能力",
            "部署运维",
            "系统适配",
            "验收记录",
            "后续规划",
            "历史归档",
        ):
            with self.subTest(category=category):
                self.assertTrue((ROOT / "docs" / category).is_dir())

    def test_retired_documents_are_archived(self):
        for retired_path in (
            ROOT / "AgentBridge_DESIGN.md",
            ROOT / "AgentBridge_DESIGN_ZH.md",
            ROOT / "docs" / "oa-write-safety.md",
            ROOT / "docs" / "oa-write-discovery.md",
        ):
            with self.subTest(path=retired_path.relative_to(ROOT)):
                self.assertFalse(retired_path.exists())

        for archived_path in (
            ROOT / "docs" / "历史归档" / "旧浏览器桥设计.md",
            ROOT / "docs" / "历史归档" / "旧协同办公写入安全模型.md",
            ROOT / "docs" / "历史归档" / "旧协同办公写入探索记录.md",
            ROOT / "docs" / "历史归档" / "旧浏览器桥退役记录.md",
            ROOT / "docs" / "历史归档" / "部署演进记录-2026年7月至8月.md",
        ):
            with self.subTest(path=archived_path.relative_to(ROOT)):
                self.assertTrue(archived_path.exists())

    def test_current_write_guides_do_not_publish_retired_commands(self):
        for relative_path in (
            "docs/架构设计/受控写入模型.md",
            "docs/系统适配/协同办公系统/写入能力扩展手册.md",
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            for forbidden in (
                "oa__",
                "agentbridge daemon",
                "chrome_extension",
                "browser_bridge_used",
            ):
                with self.subTest(document=relative_path, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_documentation_names_and_titles_are_chinese(self):
        for path in (ROOT / "docs").rglob("*"):
            relative = path.relative_to(ROOT / "docs")
            name = path.stem if path.is_file() else path.name
            with self.subTest(path=relative):
                self.assertNotRegex(name, r"[A-Za-z]")

        paths = [
            ROOT / "README.md",
            *(ROOT / "docs").rglob("*.md"),
            ROOT / "integrations" / "openclaw-agentbridge" / "README.md",
        ]
        for path in paths:
            first_heading = next(
                (
                    line
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                ),
                "",
            )
            with self.subTest(document=path.relative_to(ROOT)):
                self.assertRegex(first_heading, r"[\u4e00-\u9fff]")


if __name__ == "__main__":
    unittest.main()
