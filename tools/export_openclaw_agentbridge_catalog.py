from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.planning_policy import planning_capability_for_tool, planning_descriptor
from agentbridge.mcp.central import (
    create_central_mcp_server,
    validate_central_mcp_server_config,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "integrations"
    / "openclaw-agentbridge"
    / "lib"
    / "agentbridge-tools.json"
)


def build_catalog() -> dict:
    with TemporaryDirectory() as temp_dir:
        store = McpIdentityTokenStore(Path(temp_dir) / "agentbridge.db")
        identity = store.issue(
            user_subject="catalog-export",
            expected_principal_ref="Catalog Export",
            ttl_seconds=3600,
        )
        config = validate_central_mcp_server_config(
            host="127.0.0.1",
            port=8790,
            public_base_url="http://testserver",
            tls_cert=None,
            tls_key=None,
        )
        server = create_central_mcp_server(
            service=MagicMock(),
            identity_store=store,
            config=config,
            auth_card_base_url="http://127.0.0.1:8780",
        )
        with TestClient(server.streamable_http_app()) as client:
            response = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {identity['token']}",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "catalog-export",
                    "method": "tools/list",
                    "params": {},
                },
            )
            response.raise_for_status()
            tools = [
                tool
                for tool in response.json()["result"]["tools"]
                if not str(tool.get("name") or "").startswith("agentbridge_host_")
            ]

    return {
        "schemaVersion": "agentbridge.openclaw-tool-catalog.v1",
        "userTurnSourceTools": [
            tool["name"] for tool in tools
            if "business_source" in (
                planning_descriptor(planning_capability_for_tool(tool["name"])) or {}
            ).get("roles", [])
        ],
        "tools": tools,
    }


def serialized_catalog() -> str:
    return json.dumps(
        build_catalog(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export AgentBridge MCP tools for the native OpenClaw proxy.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    expected = serialized_catalog()
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != expected:
            print(f"AgentBridge OpenClaw tool catalog is stale: {output}")
            return 1
        print(f"AgentBridge OpenClaw tool catalog is current: {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    print(f"Exported AgentBridge OpenClaw tool catalog: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
