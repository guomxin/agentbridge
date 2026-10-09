"""Generate code facts only. Deployment and business acceptance require receipts."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def code_facts():
    from agentbridge.core.central_service import CentralCapabilityService
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config

    with TemporaryDirectory() as tmp:
        service = CentralCapabilityService(home=Path(tmp), base_url="https://oa.example.test")
        config = validate_central_mcp_server_config(host="127.0.0.1", port=8790,
            public_base_url="http://testserver", tls_cert=None, tls_key=None)
        server = create_central_mcp_server(service=service,
            identity_store=McpIdentityTokenStore(service.db_path), config=config,
            auth_card_base_url="http://127.0.0.1:8780")
        specs = service.registry.list()
        systems = {
            system: dict(sorted(Counter(s.effect for s in specs if s.system == system).items()))
            for system in sorted({s.system for s in specs})
        }
        return {"schema": "agentbridge.code-facts.v1", "basis": "constructed_registry",
            "pluginVersion": json.loads((ROOT / "integrations/openclaw-agentbridge/package.json").read_text())["version"],
            "capabilityTotal": len(specs), "systems": systems,
            "mcpToolTotal": len(server._tool_manager.list_tools()),
            "deployment": "not_asserted", "automaticValidation": "not_asserted",
            "businessAcceptance": "not_asserted"}


def render(facts):
    lines = ["<!-- BEGIN GENERATED CODE FACTS -->", "代码事实由 `scripts/current_facts.py` 从本提交注册表生成；部署、自动验证及真实业务验收分别以带日期的验收记录为准。", "",
        f"当前插件版本：`{facts['pluginVersion']}`。", "",
        "| 系统标识 | 读取 | 可逆写 | 受控写 | 合计 |", "| --- | ---: | ---: | ---: | ---: |"]
    for system, counts in facts["systems"].items():
        values = [counts.get(effect, 0) for effect in ("read", "reversible_write", "controlled_write")]
        lines.append(f"| {system} | " + " | ".join(map(str, [*values, sum(values)])) + " |")
    lines += ["", f"中央能力注册表共 {facts['capabilityTotal']} 个业务能力。",
        f"本期中央目录共有 {facts['mcpToolTotal']} 个 MCP 工具。", "<!-- END GENERATED CODE FACTS -->"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    facts = code_facts()
    if not (args.write or args.check):
        print(json.dumps(facts, ensure_ascii=False, indent=2))
        return
    path = ROOT / "docs/项目当前状态.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("<!-- BEGIN GENERATED CODE FACTS -->")
    end = text.index("<!-- END GENERATED CODE FACTS -->", start) + len("<!-- END GENERATED CODE FACTS -->")
    updated = text[:start] + render(facts) + text[end:]
    if args.write:
        path.write_text(updated, encoding="utf-8")
    elif text != updated:
        raise SystemExit("Current code facts drifted; run scripts/current_facts.py --write")


if __name__ == "__main__":
    main()
