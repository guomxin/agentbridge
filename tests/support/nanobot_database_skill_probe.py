"""Exercise original skill, immutable database source and real central checks with a stubbed SQL executor."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from agentbridge.database.independent import IndependentDatabase
from tests.database_fixtures import configured_source
from tests.support.business_draft_acceptance import OWNER
from tests.support.nanobot_controlled_write_probe import run


def exercise(
    *, fixture, binding, instance, gateway, model, skill="log-review", profile="review"
):
    source = "fixture_logs"
    other = "other_logs"
    database = IndependentDatabase(fixture.service.home)
    for sid in (source, other):
        configured_source(fixture.service.home, sid)
        database.grants.set(
            OWNER, ["database.directory", "database.logs.query"], source_id=sid
        )
    rev = fixture.service.skills.config("user:" + OWNER)["revision"]
    fixture.service.skills.save(
        "user:" + OWNER,
        {skill: {"profiles": [profile], "source_id": source}},
        expected_revision=rev,
        actor="fixture",
        reason="source-scope test",
    )
    expected_version = fixture.service.skills.current(skill)["manifest"]["version"]
    received = []

    def reply(data):
        calls = [m for m in data["messages"] if m.get("role") == "tool"]

        def ask(tool, args):
            return None, {
                "id": uuid4().hex,
                "type": "function",
                "function": {"name": "mcp_ab_" + tool, "arguments": json.dumps(args)},
            }

        if not calls:
            return ask(
                "agentbridge_skill_get",
                {
                    "skill_id": skill,
                    "expected_version": expected_version,
                    "profile": profile,
                    "source_id": source,
                },
            )
        text = str(calls[-1]["content"])
        if len(calls) == 1:
            assert "references/evidence.md" in text and "SKILL.md" in text
            return ask("database_capabilities", {})
        if len(calls) == 2:
            assert (
                source in text and other not in text and "database.logs.query" in text
            )
            return ask(
                "database_execute",
                {
                    "capability": "database.logs.query",
                    "source_id": other,
                    "arguments": {"log_id": 2},
                },
            )
        if len(calls) == 3:
            assert "中央服务未返回" in text
            return ask(
                "database_execute",
                {
                    "capability": "database.logs.query",
                    "source_id": source,
                    "arguments": {"log_id": 2},
                },
            )
        assert "合成日志来源核对" in text
        received.append(True)
        return "已核对合成日志来源。", None

    model.reply = reply
    called = []

    def sql(_runtime, subject, capability, arguments, grant, source_id):
        assert (subject, capability, source_id, arguments) == (
            OWNER,
            "database.logs.query",
            source,
            {"log_id": 2},
        )
        called.append(source_id)
        return {
            "query_id": "synthetic",
            "sql_sha256": "synthetic",
            "truncated": False,
            "rows": [{"content": "合成日志来源核对"}],
        }

    with patch.object(IndependentDatabase, "_execute", sql):
        for _ in gateway.send_dispatch(
            {"idempotency_key": "source-" + uuid4().hex},
            binding,
            "用日志复核助手读取本人合成日志。",
            [],
            None,
        ):
            pass
    assert received == [True] and called == [source], (
        "actual model checks",
        len(received),
        len(called),
    )
    return {
        "skill": skill,
        "fixedSource": True,
        "requiredResources": 2,
        "wrongSourceDenied": True,
        "centralCallsAtSelectedSource": 1,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", type=Path, required=True)
    from functools import partial

    args = p.parse_args()
    for skill, profile in (
        ("log-review", "review"),
        ("issue-tracking", "track"),
        ("similar-cases", "search"),
    ):
        print(
            json.dumps(
                run(
                    args.nanobot_python,
                    False,
                    skill_scope=True,
                    exercise=partial(exercise, skill=skill, profile=profile),
                )
            ),
            flush=True,
        )
