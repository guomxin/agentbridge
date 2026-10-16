"""HTTP/Workspace regression of all 28 actions, with exact fixture boundaries.

This is a coverage gate, not a claim that all OA DOM write adapters were tested:
eight complex OA actions retain the existing narrow synthetic adapter bindings.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from urllib.parse import urlencode
from uuid import uuid4

from tests.support.action_matrix_acceptance import (
    ActionMatrixAcceptanceFixture, MATRIX_CAPABILITIES, FIELDLESS, SYNTHETIC_BINDINGS,
)
from tests.support.full_business_acceptance import ALL_PENDING_PROFILES, OWNER

OA_EVIDENCE = "Real OA adapter, complete material parser, Central/draft/authorization/coordinator and exact done readback; synthetic downstream HTTP/DOM only"


def effects(fixture):
    return (len(fixture.matrix["effects"]) + len(fixture.smartlight.data["writes"])
            + fixture.transport.write_calls + len(fixture.oa.writes))


def seed_cases(fixture, group):
    if group in {"all", "oa"}:
        for profile in ALL_PENDING_PROFILES:
            fixture.add_pending_case(profile)
    if group in {"all", "other"}:
        for capability in MATRIX_CAPABILITIES:
            fixture.add_matrix_case(capability)
    if group == "mixed":
        fixture.add_pending_batch(ALL_PENDING_PROFILES)
    elif group == "legacy":
        fixture.add_pending_batch(("missed_punch", "missed_punch"), legacy=True)


class MatrixHttpWorkload:
    def __init__(self, fixture, client_class, password):
        self.fixture = fixture
        self.cards, self.workspace = client_class(fixture.card_url), client_class(fixture.workspace_url)
        status, _, _ = self.workspace.json("POST", "/api/login", {"username": "cardreview", "password": password})
        if status != 200:
            raise AssertionError("Workspace matrix login failed")

    def _drive(self, case):
        interaction_id = case["input_interaction"]
        for _ in range(5):
            result = self.fixture.resume(interaction_id)
            interaction = result.get("interaction")
            if interaction and interaction["resume"]["ready"]:
                interaction_id = interaction["interactionId"]
            else:
                return result
        raise AssertionError("Original Central coordinator did not settle")

    @staticmethod
    def values(state):
        raw = {}
        for field in state["ui"]["fields"]:
            name, control = field["name"], field["control"]
            raw[name] = ("2026-10-21" if control == "date" else
                "2026-10-21T10:00" if control == "datetime-local" and name == "start_time" else
                "2026-10-21T11:00" if control == "datetime-local" else
                str(field["options"][0]["value"]) if control in {"select", "segmented"} and field.get("options") else
                "2" if control == "number" else "隔离动作矩阵性能验收")
        if "project" in raw:
            raw["project"] = "合成验收项目"
        if state["ui"]["viewKind"] == "meeting_room":
            raw["room"] = ""
        return raw

    def consume(self, case):
        before = effects(self.fixture)
        started = time.monotonic()
        if "authorization_id" in case:
            path = "/flow/authorize/" + case["authorization_id"]
            status, page, opened = self.cards.request("GET", path)
            if status != 200:
                raise AssertionError((status, page))
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            status, _, approval = self.cards.request("POST", path, form=urlencode({"csrf_token": csrf, "decision": "approve"}))
            if status != 200:
                raise AssertionError("Original fieldless authorization failed")
            authorization_id = case["authorization_id"]
            timings = {"cardGetSeconds": opened, "approveSeconds": approval}
            expected_decisions = 0
        else:
            path = "/flow/input/" + case["input_id"]
            status, page, opened = self.cards.request("GET", path)
            if status != 200:
                raise AssertionError((status, page))
            config = json.loads(re.search(r"mountBusinessDraft\((\{[^\n]+\}), document, window\);", page)[1])
            token, state = config["csrfToken"], config["state"]
            raw = self.values(state)
            status, state, saved = self.cards.json("POST", path + "/draft/values",
                {"expectedRevision": state["revision"], "values": raw}, draft_token=token)
            if status != 200:
                raise AssertionError((status, state))
            if state["ui"]["viewKind"] == "meeting_room":
                status, state, _ = self.cards.json("POST", path + "/draft/options",
                    {"expectedRevision": state["revision"]}, draft_token=token)
                if status != 200:
                    raise AssertionError((status, state))
                raw["room"] = state["options"]["items"][0]["value"]
                status, state, _ = self.cards.json("POST", path + "/draft/values",
                    {"expectedRevision": state["revision"], "values": raw}, draft_token=token)
                if status != 200:
                    raise AssertionError((status, state))
            status, state, previewed = self.cards.json("POST", path + "/draft/preview",
                {"expectedRevision": state["revision"]}, draft_token=token)
            if status != 200:
                raise AssertionError((status, state))
            payload = {"expectedRevision": state["revision"], "previewId": state["preview"]["previewId"],
                       "requestKey": "matrix-http-" + uuid4().hex}
            status, decision, approved = self.cards.json("POST", path + "/draft/approve", payload, draft_token=token)
            if status != 200:
                raise AssertionError((status, decision))
            repeated_status, repeated, _ = self.cards.json("POST", path + "/draft/approve", payload, draft_token=token)
            if repeated_status != 200 or repeated["receipt"] != decision["receipt"]:
                raise AssertionError("Repeated final decision changed its receipt")
            authorization_id = decision["receipt"]["authorizationId"]
            timings = {"cardGetSeconds": opened, "saveSeconds": saved, "previewSeconds": previewed, "approveSeconds": approved}
            expected_decisions = 1
        if effects(self.fixture) != before:
            raise AssertionError("Card handler crossed the business write boundary")
        resume_start = time.monotonic()
        result = self._drive(case)
        timings["resumeSeconds"] = time.monotonic() - resume_start
        authorization = self.fixture.service.write_authorizations.get(authorization_id)
        operation = self.fixture.service.operations.get(authorization["commit_operation_id"])
        if operation["status"] != "succeeded" or authorization["state"] != "consumed":
            raise AssertionError((operation["status"], authorization["state"]))
        verification = operation["result"].get("verification", {})
        if not (verification.get("confirmed") is True or verification.get("matched") is True):
            raise AssertionError("Successful matrix result lacks authoritative readback")
        if effects(self.fixture) != before + 1:
            raise AssertionError("Expected exactly one downstream effect")
        self._drive(case)
        if effects(self.fixture) != before + 1:
            raise AssertionError("Resume replayed a downstream effect")
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM business_draft_decisions WHERE authorization_id=?",
                                       (authorization_id,)).fetchone()[0]
            if count != expected_decisions:
                raise AssertionError("Unexpected number of exact draft decisions")
        status, _, projected = self.workspace.json("GET", "/api/tasks/" + case["task_id"])
        if status != 200:
            raise AssertionError("Workspace could not project the completed business task")
        timings["workspaceProjectionSeconds"] = projected
        return {"profile": case["profile"], "capability": operation["capability_name"], "status": "succeeded",
                "writes": 1, "decisions": expected_decisions, "verificationMethod": verification.get("method"),
                "evidenceBoundary": case.get("evidence_boundary", OA_EVIDENCE),
                "totalSeconds": time.monotonic() - started, **timings}, result

    def run(self, group="all"):
        seed_cases(self.fixture, group)
        cases = list(self.fixture.meta["full_cases"]) + list(self.fixture.meta["matrix_cases"])
        rows = []
        for original in cases:
            case = original
            count = len(case.get("affair_ids", [])) or 1
            for ordinal in range(count):
                row, result = self.consume(case)
                rows.append({**row, "ordinal": ordinal + 1, "items": count})
                if ordinal + 1 < count:
                    case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                            "input_interaction": result["interaction"]["interactionId"]}
        if group == "all":
            for profiles, legacy in ((ALL_PENDING_PROFILES, False), (("missed_punch", "missed_punch"), True)):
                case = self.fixture.add_pending_batch(profiles, legacy=legacy)
                for ordinal in range(len(profiles)):
                    row, result = self.consume(case)
                    rows.append({**row, "ordinal": ordinal + 1, "items": len(profiles)})
                    if ordinal + 1 < len(profiles):
                        case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                                "input_interaction": result["interaction"]["interactionId"]}
        return {"rows": rows, "uniqueActions": len({row["capability"] for row in rows}),
                "decisions": sum(row["decisions"] for row in rows), "writes": effects(self.fixture),
                "evidenceBoundary": "Real HTTP cards and Workspace projection for all cases; 14 OA material adapters, room/Taihua/Smartlight real adapters with synthetic IO; eight other OA narrow synthetic prepare/commit bindings."}
