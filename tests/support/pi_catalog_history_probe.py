"""Real Pi and central reads for empty/paginated OA history and authorized overlapping skill names."""

import argparse
import json
from pathlib import Path
from uuid import uuid4
from unittest import TestCase

from agentbridge.core.oa_history_queries import OaHistoryQueryError
from agentbridge.workspace.oa_history import _binding

from tests.authorization_fixtures import grant_permissions
from tests.support.pi_governed_probe import exercise
from tests.support.pi_skills_probe import chat
from tests.test_oa_history_query_service import row

NEXT = "oa_workflow_history_next"
QUERY = "oa_workflow_history_query"
CATALOG = "agentbridge_skill_catalog"
LOAD = "agentbridge_skill_get"
EXPLICIT = "weekly-candidate-14"


def tool(name, args):
    return None, {
        "id": "read-" + uuid4().hex,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def packet(raw):
    if isinstance(raw, list):
        raw = "".join(p.get("text", "") for p in raw)
    return json.loads(raw)


class Model:
    def __init__(self, central):
        self.failures = []
        self.mode = ""
        self.reports = []
        self.catalogs = []
        self.loads = []
        self.page_check = lambda page: None
        self.limit = 1

    def reply(self, body):
        last = body["messages"][-1]
        names = [t["function"]["name"] for t in body.get("tools", [])]
        if self.mode in ("empty", "pages", "source_pages", "browser"):
            assert QUERY in names and NEXT in names
            if last["role"] != "tool":
                return tool(QUERY, {"collection": "done", "temporal": {"kind": "all"}, "limit": self.limit})
            item = packet(last["content"])
            assert item["status"] == "succeeded", item.get("error", {}).get("code")
            page = item["result"]
            self.page_check(page)
            self.reports.append(page)
            if self.mode == "browser":
                return "已取本人历史第一页，可在任务卡上翻页。", None
            if page["has_more"]:
                return tool(NEXT, {"cursor": page["next_cursor"]})
            assert page["state"] == "complete"
            rows = [r["title"] for p in self.reports for r in p["items"]]
            return "符合条件的历史事项：" + ("、".join(rows) if rows else "无"), None
        assert CATALOG in names
        if last["role"] != "tool":
            return tool(CATALOG, {})
        item = packet(last["content"])
        if "items" in item:
            rows = item["items"]
            self.catalogs.append([r["id"] for r in rows])
            if self.mode == "unassigned":
                assert not rows
                return "本人没有获配这些助手。", None
            namesakes = [r for r in rows if r["name"] == "周报整理助手"]
            assert len(namesakes) == 2
            if self.mode == "ambiguous":
                return "有两位同名候选，请指定助手编号。", None
            assert self.mode == "explicit" and EXPLICIT in [r["id"] for r in namesakes]
            selected = next(r for r in namesakes if r["id"] == EXPLICIT)
            return tool(
                LOAD, {"skill_id": selected["id"], "profile": "use", "expected_version": selected["version"]}
            )
        assert item.get("skill_id") == EXPLICIT and item["status"] == "succeeded"
        self.loads.append((item["skill_id"], item["version"]))
        return "已按编号加载指定周报候选。", None


class Scenario:
    def __init__(self, part):
        self.part = part

    def model(self, central):
        model = Model(central)
        svc = central.service
        subject = central.subjects["a"]
        grants = set(svc.user_grants.get(subject)["permissions"])
        grant_permissions(svc.user_grants, subject, grants | {"oa.workflow.read"})
        self.source, self.visits = [], []

        def read_page(adapter, worker, *, collection, page_number, temporal):
            self.visits.append(page_number)
            return {
                "page": page_number,
                "size": 50,
                "pages": (len(self.source) + 49) // 50,
                "total": len(self.source),
                "items": self.source[(page_number - 1) * 50 : page_number * 50],
            }

        svc.oa_history_query_service.read_page = read_page

        def reject_detail(*args, **kwargs):
            raise AssertionError("ended test rows already have an authoritative node state")

        svc.oa_history_query_service.read_nodes = reject_detail
        if self.part == "browser":
            self.source.extend(row(i, title=f"合成事项{i}") for i in (1, 2, 3))
            model.mode = "browser"
        return model

    def check(self, central, post, model, accounts):
        svc = central.service
        a, b = central.subjects["a"], central.subjects["b"]
        results = {}
        cases = [] if self.part == "skills" else [("empty", 0, 1), ("pages", 3, 1), ("source_pages", 51, 25)]
        for mode, count, limit in cases:
            self.source[:] = [row(i, title=f"合成事项{i}") for i in range(1, count + 1)]
            self.visits.clear()
            model.mode, model.reports, model.limit = mode, [], limit
            with svc.tasks._connect() as db:
                start = db.execute("SELECT COALESCE(MAX(rowid),0) FROM operations").fetchone()[0]

            def records():
                with svc.tasks._connect() as db:
                    return db.execute(
                        """SELECT t.task_id,t.user_subject,t.origin_endpoint_id,t.agent_host,
                        t.status AS task_status,t.current_operation_id,o.operation_id,o.capability_name,
                        o.status,json_extract(o.result_json,'$.query_id') AS query_id
                        FROM operations o JOIN task_operations l ON l.operation_id=o.operation_id
                        JOIN agent_tasks t ON t.task_id=l.task_id WHERE o.rowid>? ORDER BY o.rowid""",
                        (start,),
                    ).fetchall()

            def check_page(page):
                ops = records()
                latest = ops[-1]
                assert latest["query_id"] == page["query_id"]
                assert latest["task_status"] == ("running" if page["has_more"] else "succeeded")
                assert latest["current_operation_id"] == latest["operation_id"]
                if not page["has_more"]:
                    return
                before = list(self.visits)
                task = latest["task_id"]
                for stale in ["non-current", *[p["next_cursor"] for p in model.reports if p["has_more"]]]:
                    with TestCase().assertRaises(PermissionError):
                        svc.invoke(
                            user_subject=a,
                            host_type="pi",
                            task_id=task,
                            capability_name="oa.workflow.history.next",
                            arguments={"cursor": stale},
                        )
                with TestCase().assertRaises(PermissionError):
                    svc.invoke(
                        user_subject=a,
                        host_type="pi",
                        task_id=task,
                        capability_name="oa.workflow.history.query",
                        arguments={"collection": "done", "temporal": {"kind": "all"}},
                    )
                for foreign_task in (None, "other-task"):
                    with TestCase().assertRaises(OaHistoryQueryError) as error:
                        svc.oa_history_query_service.next(
                            cursor=page["next_cursor"],
                            adapter=None,
                            worker=None,
                            pi_task_id=foreign_task,
                            **_binding(svc, a),
                        )
                    assert error.exception.code == "OA_HISTORY_TASK_CHANGED"
                assert list(self.visits) == before and len(records()) == len(ops)

            model.page_check = check_page
            answer = chat(post, accounts["a"], f"查询所有本人已办历史，每页{limit}条。")
            received = [r["title"] for p in model.reports for r in p["items"]]
            expected = [r["title"] for r in self.source]
            assert received == expected and (
                all(t in answer for t in expected) if expected else answer.endswith("无")
            )
            assert not model.reports[-1]["has_more"] and not model.reports[-1]["next_cursor"]
            ops = records()
            task = ops[0]["task_id"]
            assert [r["capability_name"] for r in ops] == ["oa.workflow.history.query"] + [
                "oa.workflow.history.next"
            ] * (len(model.reports) - 1)
            assert all(
                r["task_id"] == task
                and r["agent_host"] == "pi"
                and r["user_subject"] == a
                and r["origin_endpoint_id"] == accounts["a"]["endpoint_id"]
                and r["status"] == "succeeded"
                and r["query_id"] == model.reports[0]["query_id"]
                for r in ops
            )
            before = list(self.visits)
            svc.observe_host_task(user_subject=a, task_id=task, operation_ids=[ops[0]["operation_id"]])
            current = svc.tasks.get_task(task, user_subject=a)
            assert (
                current["current_operation_id"] == ops[-1]["operation_id"]
                and current["status"] == "succeeded"
            )
            with TestCase().assertRaises(PermissionError):
                svc.invoke(
                    user_subject=a,
                    host_type="pi",
                    task_id=task,
                    capability_name="oa.workflow.history.next",
                    arguments={"cursor": model.reports[0].get("next_cursor") or "non-current"},
                )
            with TestCase().assertRaises((KeyError, PermissionError)):
                svc.invoke(
                    user_subject=b,
                    host_type="pi",
                    task_id=task,
                    capability_name="oa.workflow.history.next",
                    arguments={"cursor": model.reports[0].get("next_cursor") or "non-current"},
                )
            assert len(records()) == len(ops) and self.visits == before
            if mode == "source_pages":
                assert set(self.visits) == {1, 2}
            results[mode] = {
                "rows": len(received),
                "cursorContinuations": len(model.reports) - 1,
                "centralOperations": len(ops),
                "sameOriginalTask": True,
                "oldCursorAdditionalReads": 0,
                "terminalCoverage": model.reports[-1]["state"],
                "sourcePageNumbers": sorted(set(self.visits)),
            }
        assert (
            not model.failures
            and not central.matrix.matrix["effects"]
            and not central.matrix.oa.writes
            and not central.matrix.smartlight.data["writes"]
        )
        if self.part == "history":
            return {"history": results, "businessWrites": 0}
        store = svc.skills
        items = {}
        for i in range(15):
            name = "周报整理助手" if i in (12, 14) else "周报辅助候选" + str(i)
            sid = "weekly-candidate-" + str(i).zfill(2)
            bundle = svc.skill_authoring.normalize(
                sid,
                {
                    "name": name,
                    "description": "整理本人合成的周报材料。",
                    "selection": {"use_when": "归纳周报", "not_for": "普通润色", "output": "完成与计划"},
                    "instructions": "只从本轮合成材料提取事项，版本标记 " + sid + "。",
                    "profiles": {"use": {}},
                },
            )
            items[sid] = bundle
        store.registry.items.update(items)
        with store._connect() as db:
            for sid, bundle in items.items():
                db.execute(
                    "INSERT INTO skill_versions VALUES (?,?,?,?)",
                    (sid, bundle["manifest"]["version"], bundle["content_hash"], json.dumps(bundle)),
                )
        store.save(
            "user:" + a,
            {sid: {"profiles": ["use"]} for sid in items},
            expected_revision=store.config("user:" + a)["revision"],
            actor="fixture",
            reason="compare synthetic overlapping names",
        )
        ranked = svc.skill_authoring.workbench.discover(a, "请使用 " + EXPLICIT, limit=3)
        assert ranked["total"] == 15 and ranked["has_more"] and ranked["items"][0]["id"] == EXPLICIT
        assert not svc.skill_authoring.workbench.discover(b, "请使用 " + EXPLICIT, limit=3)["items"]
        for m, subject, prompt in (
            ("ambiguous", "a", "请用周报整理助手。"),
            ("explicit", "a", "请使用 " + EXPLICIT + "。"),
            ("unassigned", "b", "请用同一周报整理助手。"),
        ):
            model.mode = m
            answer = chat(post, accounts[subject], prompt)
            assert {"ambiguous": "两位", "explicit": "按编号", "unassigned": "没有获配"}[m] in answer
        assert model.catalogs[0].index(EXPLICIT) >= 12 and model.catalogs[-1] == [] and len(model.loads) == 1
        assert (
            not model.failures
            and not central.matrix.matrix["effects"]
            and not central.matrix.oa.writes
            and not central.matrix.smartlight.data["writes"]
        )
        return {
            "history": results,
            "authorizedSkills": 15,
            "sameNames": 2,
            "explicitCatalogPosition": 1 + model.catalogs[0].index(EXPLICIT),
            "explicitRank": 1,
            "expectedRevisionLoaded": True,
            "unassignedCatalog": 0,
            "businessWrites": 0,
            "syntheticModel": True,
            "syntheticDownstream": True,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for k in ("node", "sdk-dir", "engine-python"):
        p.add_argument("--" + k, required=True, type=Path)
    p.add_argument("--case", choices=("all", "skills", "history", "browser"), default="all")
    a = p.parse_args()
    print(
        json.dumps(
            exercise(
                a.node,
                a.sdk_dir,
                a.engine_python,
                "history_skills",
                scenario=Scenario(a.case),
                serve=a.case == "browser",
            )
        ),
        flush=True,
    )
