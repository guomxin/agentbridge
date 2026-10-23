"""Independent OA verification/performance workload with closed synthetic IO.

Central, Auth, Workspace, the adapter, material checks and verifier are real.
The same module may be loaded by path with imports resolving to a frozen source
tree. Only downstream IO and an explicitly identical execution timeout differ
from production; no verifier result, phase event or ledger state is fabricated.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_execution import execute_pending_action
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, FullOaPage, FullOaTransport, FullOaWorker, OWNER
from tests.support.interaction_acceptance import write_metadata
from tests.support.latency_acceptance import JsonHttpClient, distribution
from tests.test_workspace import PASSWORD


CONTRACT = "oa-verification-isolated-v1"
SCENARIOS = ("first-page", "second-page", "budget", "material-drift")


class VerificationContract:
    def __init__(self, *, execution_timeout=1.2, io_delay=.003, fixed_wait_scale=.01, observable=False):
        self.execution_timeout = float(execution_timeout)
        self.io_delay = float(io_delay)
        self.fixed_wait_scale = float(fixed_wait_scale)
        self.observable = bool(observable)
        if not .1 <= self.execution_timeout <= 30 or not 0 <= self.io_delay <= 5 or not 0 <= self.fixed_wait_scale <= 1:
            raise ValueError("Synthetic timing controls exceed their explicit bounds")

    def public(self):
        return {"requestedExecutionTimeoutSeconds": self.execution_timeout, "downstreamIoDelaySeconds": self.io_delay,
            "fixedWaitScale": self.fixed_wait_scale, "observable": self.observable,
            "boundary": "Real Auth/Workspace HTTP + Central and adapter; closed synthetic OA HTTP/DOM; no browser/model startup measurement"}


class VerificationTransport(FullOaTransport):
    def __init__(self, path, contract):
        super().__init__(path)
        self.contract = contract
        self.data.setdefault("verification_cases", {})
        self.data.setdefault("verification_io", [])
        self.current_target = None

    def scenario(self, affair_id):
        return self.data["verification_cases"].get(affair_id or self.current_target, {})

    def wait(self, kind, *, affair_id=None, timeout_ms=None, delay=None, **metadata):
        delay = self.contract.io_delay if delay is None else max(float(delay), 0)
        allowed = max(float(timeout_ms) / 1000, 0) if timeout_ms is not None else None
        started = time.monotonic()
        failed = allowed is not None and delay > allowed
        time.sleep(min(delay, allowed) if failed else delay)
        event = {"kind": kind, "affairId": affair_id, "startedMonotonic": started,
            "elapsedSeconds": time.monotonic() - started, "requestedTimeoutMs": timeout_ms,
            "requestedDelaySeconds": delay, "timedOut": failed, **metadata}
        with self.lock:
            self.data["verification_io"].append(event)
        if failed:
            raise TimeoutError("Closed synthetic IO exceeded the supplied remaining timeout")

    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        args = parse_qs(parsed.query)
        kind = "pending_http" if args.get("managerName") == ["sectionManager"] else "template_http"
        self.wait(kind, affair_id=self.current_target, timeout_ms=kwargs.get("timeout_seconds", 0) * 1000 or None)
        return super().request(method, url, **kwargs)

    def history(self, target_id):
        case = self.scenario(target_id)
        target = self.data["affairs"].get(target_id)
        count = int(case.get("history_total", 1))
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        rows = [{"affair_id": f"history-{target_id}-{i}", "title": f"独立历史事项 {i}",
            "date": day + " 10:00", "date_basis": "processed_at", "sender": "合成历史申请人", "category": "协同"}
            for i in range(count)]
        if target and target["processed"] and not case.get("omit_target"):
            index = int(case.get("target_index", 0))
            if index < len(rows):
                rows[index] = {**deepcopy(target["source"]), "date": day + " 11:00", "date_basis": "processed_at"}
        return rows


class VerificationPage(FullOaPage):
    def __init__(self, worker):
        super().__init__(worker)
        self.ready_started = None
        self.dispatch_state = "not_invoked"

    def evaluate(self, script, argument=None):
        transport = self.worker.transport
        target_id = self.worker.affair_id or self.worker.target_id or transport.current_target
        case = transport.scenario(target_id)
        if "agentbridge_pending_readiness" in script:
            if self.ready_started is None:
                self.ready_started = time.monotonic()
            elapsed = time.monotonic() - self.ready_started
            hold = float(case.get("ready_delay", 0))
            ready = elapsed >= hold
            if ready and case.get("drift_during_readiness") and not case.get("drift_applied"):
                transport.data["affairs"][target_id]["body_text"] += "提交前正文已经变化，必须重新核对。"
                case["drift_applied"] = True
                transport.persist()
            return {"identity_matches": True, "ready": ready, "body_loaded": ready,
                "content_api_loaded": ready, "submit_entry": "submitClickFunc", "reason": "ready" if ready else "body_loading"}
        if "agentbridge_pending_dispatch_observation" in script:
            return {"state": self.dispatch_state}
        if isinstance(argument, dict) and "opinion" in argument:
            result = super().evaluate(script, argument)
            self.dispatch_state = "invoked"
            return result
        if isinstance(argument, dict) and "managerMethod" in argument:
            required = {"managerMethod", "filters", "pageNumber", "pageSize", "dateField", "dateBasis", "timeoutMs"}
            if set(argument) != required or argument["managerMethod"] != "getDoneList" or argument["pageSize"] != 50:
                raise AssertionError("Only the verified getDoneList page contract is allowed")
            filters = argument["filters"]
            if set(filters) not in ({"dealDate", "dumpData", "deduplication", "aiProcessing"}, {"dumpData", "deduplication", "aiProcessing"}) or any(
                    filters[key] != "false" for key in ("dumpData", "deduplication", "aiProcessing")):
                raise AssertionError("Synthetic history rejects unsupported or broadened query filters")
            if argument["dateField"] != "dealTime" or argument["dateBasis"] != "processed_at":
                raise AssertionError("Processed-at evidence must use Seeyon's real dealTime field")
            number = argument["pageNumber"]
            transport.wait("history_page", affair_id=target_id, page=number,
                timeout_ms=argument["timeoutMs"], delay=case.get("done_delay", transport.contract.io_delay))
            if case.get("page_fault") == "failed" and number == 2:
                raise ConnectionError("Synthetic second history page unavailable")
            rows = transport.history(target_id)
            start, end = filters.get("dealDate", "#").split("#")
            rows = [row for row in rows if (not start or row["date"][:10] >= start) and (not end or row["date"][:10] <= end)]
            total = len(rows)
            items = deepcopy(rows[(number - 1) * 50:number * 50])
            fault = case.get("page_fault")
            if number == 2:
                if fault == "total_drift":
                    total += 1
                elif fault == "duplicate" and items:
                    items[0] = deepcopy(rows[0])
                elif fault == "short":
                    items = items[:-1]
                elif fault == "date" and items:
                    items[0]["date"] = ""
                elif fault == "basis" and items:
                    items[0]["date_basis"] = "initiated_at"
            return {"items": items, "total": total, "page": number, "size": 50, "pages": math.ceil(total / 50)}
        if isinstance(argument, dict) and set(argument) == {"gridId", "dateFields"}:
            if argument["gridId"] != "listDone" or not self.worker.is_reader:
                raise AssertionError("Readback history must use its independent original done page")
            transport.wait("history_grid", affair_id=target_id,
                delay=case.get("done_delay", transport.contract.io_delay))
            rows = transport.history(target_id)
            return {"items": deepcopy(rows[:50]), "total": len(rows), "page": 1}
        return super().evaluate(script, argument)

    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        self.worker.transport.wait("event_pump", affair_id=self.worker.affair_id,
            delay=milliseconds / 1000 * self.worker.transport.contract.fixed_wait_scale)

    def wait_for_function(self, script, *, arg, timeout=None, **kwargs):
        self.worker.transport.wait("grid_ready", affair_id=self.worker.target_id,
            timeout_ms=timeout, delay=self.worker.transport.scenario(self.worker.target_id).get("grid_delay", self.worker.transport.contract.io_delay))
        return super().wait_for_function(script, arg=arg, **kwargs)


class VerificationWorker(FullOaWorker):
    def __init__(self, transport, *, is_reader=False, target_id=None):
        super().__init__(transport, is_reader=is_reader)
        self.target_id = target_id
        self.page = VerificationPage(self)

    def goto(self, url, **kwargs):
        self.transport.wait("navigation", affair_id=self.target_id,
            timeout_ms=kwargs.get("timeout_seconds", 0) * 1000 or None)
        return super().goto(url, **kwargs)

    def rendered_snapshot(self, url, **kwargs):
        target_id = parse_qs(urlparse(url).query).get("affairId", [None])[0]
        self.target_id = target_id
        self.transport.wait("material", affair_id=target_id,
            timeout_ms=kwargs.get("timeout_seconds", 0) * 1000 or None)
        return super().rendered_snapshot(url, **kwargs)

    def current_snapshot(self, **kwargs):
        case = self.transport.scenario(self.affair_id)
        self.transport.wait("final_material", affair_id=self.affair_id,
            timeout_ms=kwargs.get("timeout_seconds", 0) * 1000 or None,
            delay=case.get("final_material_delay", self.transport.contract.io_delay))
        return super().current_snapshot(**kwargs)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield VerificationWorker(self.transport, is_reader=True,
                target_id=self.affair_id or self.target_id or self.transport.current_target)
        finally:
            self.readback_closed = True


class OaVerificationAcceptanceFixture(FullBusinessAcceptanceFixture):
    def __init__(self, home: Path, *, contract=None, seed=False):
        super().__init__(home, seed=False)
        self.contract = contract or VerificationContract()
        self.oa = VerificationTransport(self.home / "synthetic-oa-views.json", self.contract)
        self.service._worker_factories_by_system["oa"] = lambda *_: VerificationWorker(self.oa)
        self.meta.setdefault("verification_cases", [])
        # Equal requested deadline for both revisions. The baseline's historical
        # minimum clamp remains observable; no verification logic is replaced.
        def bounded_execution(*args, **kwargs):
            return execute_pending_action(*args, **{**kwargs, "timeout_seconds": self.contract.execution_timeout})
        self._execution_patch = patch("agentbridge.adapters.seeyon_pending_actions.execute_pending_action", bounded_execution)
        self._execution_patch.start()
        write_metadata(self.home / "browser-credentials.json", {"username": "cardreview", "password": PASSWORD})
        if seed and not self.meta["verification_cases"]:
            for scenario in SCENARIOS:
                self.add_scenario(scenario)
        write_metadata(self.meta_path, self.meta)

    def add_scenario(self, scenario, *, key=None, **overrides):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown verification scenario")
        case = self.add_pending_case("efficiency_data", case_key=key or "verify-" + scenario + "-" + uuid4().hex[:8])
        affair = case["arguments"]["affair_id"]
        shape = {"scenario": scenario, "history_total": 53 if scenario == "second-page" else 1,
            "target_index": 51 if scenario == "second-page" else 0,
            "drift_during_readiness": scenario == "material-drift"}
        if scenario == "budget":
            shape["done_delay"] = self.contract.execution_timeout + .3
        if self.contract.observable and scenario == "first-page":
            shape.update(ready_delay=2.2, final_material_delay=1.6, done_delay=1.6)
        shape.update(overrides)
        self.oa.current_target = affair
        self.oa.data["verification_cases"][affair] = shape
        self.oa.persist()
        case = {**case, "scenario": scenario, "shape": shape}
        self.meta["verification_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def audit(self):
        with self.service.tasks._connect() as db:
            counts = {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in ("business_draft_decisions", "agent_host_dispatches")}
            auth = dict(db.execute("SELECT state,count(*) FROM write_authorizations GROUP BY state"))
            operations = dict(db.execute("SELECT status,count(*) FROM operations WHERE capability_name='oa.efficiency_data.approve' GROUP BY status"))
        return {"decisions": counts["business_draft_decisions"], "modelDispatches": counts["agent_host_dispatches"],
            "authorizations": auth, "commitOperations": operations, "syntheticWrites": len(self.oa.writes),
            "coordinatorErrors": list(self.coordinator_errors)}

    def public_metadata(self):
        return {"fixture": CONTRACT, "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "home": str(self.home), "credentials_file": str(self.home / "browser-credentials.json"),
            "cases": self.meta["verification_cases"], "contract": self.contract.public()}

    def close(self):
        try:
            super().close()
        finally:
            self._execution_patch.stop()


class VerificationWorkload:
    def __init__(self, fixture):
        self.fixture = fixture
        self.cards, self.workspace = JsonHttpClient(fixture.card_url), JsonHttpClient(fixture.workspace_url)
        status, _, _ = self.workspace.json("POST", "/api/login", {"username": "cardreview", "password": PASSWORD})
        assert status == 200, status

    def process(self, scenario):
        before = self.fixture.audit()
        offset = len(self.fixture.oa.data["verification_io"])
        started = time.monotonic()
        cpu_started = time.process_time()
        sample = {"scenario": scenario, "startedAt": datetime.now(timezone.utc).isoformat()}
        try:
            case = self.fixture.add_scenario(scenario)
            sample["taskId"] = case["task_id"]
            path = "/flow/input/" + case["input_id"]
            status, page, _ = self.cards.request("GET", path)
            assert status == 200, status
            config = json.loads(re.search(r"mountBusinessDraft\((\{[^\n]+\}), document, window\);", page)[1])
            token = config["csrfToken"]
            status, saved, _ = self.cards.json("POST", path + "/draft/values",
                {"expectedRevision": config["state"]["revision"], "values": {"opinion": "独立合成核验意见；明确且仅一次决定。"}}, draft_token=token)
            assert status == 200, saved
            status, preview, _ = self.cards.json("POST", path + "/draft/preview", {"expectedRevision": saved["revision"]}, draft_token=token)
            assert status == 200, preview
            decision = {"expectedRevision": preview["revision"], "previewId": preview["preview"]["previewId"], "requestKey": uuid4().hex}
            confirmed = time.monotonic()
            status, receipt, _ = self.cards.json("POST", path + "/draft/approve", decision, draft_token=token)
            assert status == 200, receipt
            again_status, again, _ = self.cards.json("POST", path + "/draft/approve", decision, draft_token=token)
            assert again_status == 200 and again["receipt"] == receipt["receipt"]
            auth_id = receipt["receipt"]["authorizationId"]
            interaction_id = case["input_interaction"]
            for _ in range(4):
                result = self.fixture.resume(interaction_id)
                interaction = result.get("interaction")
                if interaction and interaction["resume"]["ready"]:
                    interaction_id = interaction["interactionId"]
                else:
                    break
            status, projected, _ = self.workspace.json("GET", "/api/tasks/" + case["task_id"])
            assert status == 200, projected
            sample["confirmationToProjectionSeconds"] = time.monotonic() - confirmed
            sample["taskStatus"] = projected["task"]["status"]
            auth = self.fixture.service.write_authorizations.get(auth_id)
            sample["authorizationState"] = auth["state"]
            if auth.get("commit_operation_id"):
                operation = self.fixture.service.operations.get(auth["commit_operation_id"])
                sample["operationStatus"] = operation["status"]
                sample["verified"] = (operation.get("result") or {}).get("verification", {}).get("confirmed") is True
            else:
                sample.update(operationStatus="before_consumption", verified=False)
            # Repeating the original coordinator may read the same receipt but
            # must not invoke another business effect, including unknown cases.
            writes = len(self.fixture.oa.writes)
            self.fixture.resume(case["input_interaction"])
            assert len(self.fixture.oa.writes) == writes
        except Exception as error:
            sample["error"] = type(error).__name__ + ": " + str(error)[:500]
        sample["totalSeconds"] = time.monotonic() - started
        sample["processCpuSeconds"] = time.process_time() - cpu_started
        after = self.fixture.audit()
        sample["ledgerDelta"] = {"decisions": after["decisions"] - before["decisions"],
            "consumed": after["authorizations"].get("consumed", 0) - before["authorizations"].get("consumed", 0),
            "writes": after["syntheticWrites"] - before["syntheticWrites"],
            "models": after["modelDispatches"] - before["modelDispatches"]}
        events = self.fixture.oa.data["verification_io"][offset:]
        sample["ioCounts"] = {kind: sum(event["kind"] == kind for event in events) for kind in sorted({event["kind"] for event in events})}
        sample["timedOutCalls"] = [event for event in events if event["timedOut"]]
        sample["ioEvents"] = events
        sample["requestedSequentialDelaySeconds"] = sum(min(event["requestedDelaySeconds"], event["requestedTimeoutMs"] / 1000)
            if event["timedOut"] else event["requestedDelaySeconds"] for event in events)
        sample["actualSequentialDelaySeconds"] = sum(event["elapsedSeconds"] for event in events)
        sample["totalLessMeasuredDelaySeconds"] = sample["totalSeconds"] - sample["actualSequentialDelaySeconds"]
        sample["ledgerValid"] = sample["ledgerDelta"] == ({"decisions": 1, "consumed": 0, "writes": 0, "models": 0}
            if scenario == "material-drift" else {"decisions": 1, "consumed": 1, "writes": 1, "models": 0})
        return sample


def summarize(samples):
    result = {}
    for scenario in SCENARIOS:
        rows = [row for row in samples if row["scenario"] == scenario]
        if not rows:
            continue
        result[scenario] = {"samples": len(rows), "errors": sum("error" in row for row in rows),
            "verified": sum(row.get("verified") is True for row in rows),
            "unknown": sum(row.get("operationStatus") == "unknown" for row in rows),
            "beforeConsumption": sum(row.get("operationStatus") == "before_consumption" for row in rows),
            "invalidLedgers": sum(row.get("ledgerValid") is not True for row in rows),
            "totalSeconds": distribution([row["totalSeconds"] for row in rows]),
            "confirmationToProjectionSeconds": distribution([row["confirmationToProjectionSeconds"] for row in rows if "confirmationToProjectionSeconds" in row]),
            "processCpuSeconds": distribution([row["processCpuSeconds"] for row in rows]),
            "actualSequentialDelaySeconds": distribution([row["actualSequentialDelaySeconds"] for row in rows]),
            "requestedSequentialDelaySeconds": distribution([row["requestedSequentialDelaySeconds"] for row in rows]),
            "totalLessMeasuredDelaySeconds": distribution([row["totalLessMeasuredDelaySeconds"] for row in rows]),
            "ioCounts": {kind: distribution([row["ioCounts"].get(kind, 0) for row in rows]) for kind in sorted({key for row in rows for key in row["ioCounts"]})}}
    return result
