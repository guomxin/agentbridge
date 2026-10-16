"""Repeatable, network-free performance measurements of real business paths.

Only downstream HTTP/DOM and the optional 300 ms fixture coordinator are
synthetic.  The latter is deliberately NOT an OpenClaw notification benchmark.
This module can be loaded by file path while imports resolve against an archived
source tree, so both revisions run the same workload and latency contract.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from tests.support.full_business_acceptance import (
    FullBusinessAcceptanceFixture, FullOaTransport, FullOaWorker, FullOaPage, OWNER,
)
from tests.support.interaction_acceptance import write_metadata
from tests.test_workspace import PASSWORD

CONTRACT = "latency-isolated-http-v1"
SCENARIOS = ("query", "single", "mixed", "legacy", "drift", "unknown")


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    percentile = lambda fraction: values[max(0, math.ceil(len(values) * fraction) - 1)]
    return {"count": len(values), "median": percentile(.5), "p95": percentile(.95),
            "max": values[-1], "min": values[0]}


class LatencyContract:
    def __init__(self, *, io_delay_seconds=.02, worker_start_seconds=.01, fixed_wait_scale=0):
        for value in (io_delay_seconds, worker_start_seconds, fixed_wait_scale):
            if not 0 <= value <= 30:
                raise ValueError("Latency controls must be between 0 and 30")
        self.io_delay_seconds = io_delay_seconds
        self.worker_start_seconds = worker_start_seconds
        self.fixed_wait_scale = fixed_wait_scale
        self.events = []
        self.lock = threading.Lock()

    def pause(self, kind, seconds):
        started = time.monotonic()
        if seconds:
            time.sleep(seconds)
        with self.lock:
            self.events.append({"kind": kind, "seconds": time.monotonic() - started})

    def io(self, kind):
        self.pause(kind, self.io_delay_seconds)

    def snapshot(self):
        with self.lock:
            return len(self.events)

    def since(self, offset):
        with self.lock:
            events = self.events[offset:]
        counts, durations = {}, {}
        for event in events:
            kind = event["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            durations[kind] = durations.get(kind, 0) + event["seconds"]
        return {"counts": counts, "controlledDelaySeconds": durations}

    def public(self):
        return {"downstreamIoDelaySeconds": self.io_delay_seconds,
                "workerStartupDelaySeconds": self.worker_start_seconds,
                "fixedWaitScale": self.fixed_wait_scale,
                "readyDom": "Synthetic complete DOM is immediately available",
                "coordinator": "Fixture Central resume loop; not OpenClaw notification pump"}


class TimedTransport(FullOaTransport):
    def __init__(self, path, latency):
        super().__init__(path)
        self.latency = latency

    def request(self, *args, **kwargs):
        self.latency.io("http")
        return super().request(*args, **kwargs)

    def submit(self, *args, **kwargs):
        self.latency.io("write")
        return super().submit(*args, **kwargs)


class TimedPage(FullOaPage):
    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        self.worker.transport.latency.pause("fixedWait", milliseconds / 1000 * self.worker.transport.latency.fixed_wait_scale)


class TimedWorker(FullOaWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = TimedPage(self)

    def __enter__(self):
        self.transport.latency.pause("workerStart", self.transport.latency.worker_start_seconds)
        return super().__enter__()

    def __exit__(self, *args):
        self.transport.latency.pause("workerClose", 0)
        return super().__exit__(*args)

    def goto(self, *args, **kwargs):
        self.transport.latency.io("goto")
        return super().goto(*args, **kwargs)

    def rendered_snapshot(self, *args, **kwargs):
        self.transport.latency.io("material")
        self.transport.latency.pause("snapshotSettleWait", max(float(kwargs.get("settle_ms", 0)), 0) / 1000
                                     * self.transport.latency.fixed_wait_scale)
        return super().rendered_snapshot(*args, **kwargs)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        self.transport.latency.io("readbackOpen")
        try:
            yield TimedWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class LatencyAcceptanceFixture(FullBusinessAcceptanceFixture):
    def __init__(self, home: Path, *, latency=None, seed=True):
        super().__init__(home, seed=False)
        self.latency = latency or LatencyContract()
        self.oa = TimedTransport(self.home / "synthetic-oa-views.json", self.latency)
        self.service._worker_factories_by_system["oa"] = lambda *_: TimedWorker(self.oa)
        if "latency-query-target" not in self.oa.data["affairs"]:
            self.oa.add_affair("latency-query-target", profile="leave")
        if seed and not self.meta["full_cases"]:
            self.add_pending_case("attendance_confirmation", case_key="browser-single")
            self.add_pending_batch(("missed_punch", "weekly_report"))

    def public_metadata(self):
        return {**super().public_metadata(), "benchmarkContract": CONTRACT,
                "latencyContract": self.latency.public(), "synthetic_password": PASSWORD,
                "evidenceBoundary": "Real Central, adapters, material checks, draft/authorization ledgers, HTTP cards and Workspace; synthetic downstream. No model or real browser startup measurement."}

    def audit(self):
        with sqlite3.connect(self.service.db_path) as connection:
            def grouped(query):
                return {str(key): value for key, value in connection.execute(query)}
            return {"decisionCount": connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0],
                    "authorizationStates": grouped("SELECT state,COUNT(*) FROM write_authorizations GROUP BY state"),
                    "commitStatuses": grouped("SELECT o.status,COUNT(*) FROM write_authorizations a "
                                               "JOIN operations o ON o.operation_id=a.commit_operation_id GROUP BY o.status"),
                    "downstreamWriteCount": len(self.oa.writes),
                    "coordinatorErrors": list(self.coordinator_errors)}


class JsonHttpClient:
    """Use fresh HTTP connections identically on both source revisions."""
    def __init__(self, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise ValueError("Benchmark client only accepts isolated loopback HTTP")
        self.host, self.port, self.origin = parsed.hostname, parsed.port, origin
        self.cookies = {}

    def request(self, method, path, body=None, *, draft_token=None):
        headers = {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if body is not None:
            headers["Content-Type"] = "application/json"
        if draft_token:
            headers["X-AgentBridge-Draft-Token"] = draft_token
        started = time.monotonic()
        connection = HTTPConnection(self.host, self.port, timeout=60)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            payload = response.read().decode()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    for name, morsel in SimpleCookie(value).items():
                        self.cookies[name] = morsel.value
            return response.status, payload, time.monotonic() - started
        finally:
            connection.close()

    def json(self, method, path, body=None, *, draft_token=None):
        status, payload, elapsed = self.request(method, path, body, draft_token=draft_token)
        return status, json.loads(payload), elapsed


class HttpWorkload:
    def __init__(self, fixture):
        self.fixture = fixture
        self.cards, self.workspace = JsonHttpClient(fixture.card_url), JsonHttpClient(fixture.workspace_url)
        status, _, _ = self.workspace.json("POST", "/api/login", {"username": "cardreview", "password": PASSWORD})
        if status != 200:
            raise AssertionError(f"Workspace benchmark login failed: {status}")

    def query(self):
        started = time.monotonic()
        result = self.fixture.service.invoke(user_subject=OWNER, capability_name="oa.workflow.pending.list", arguments={},
                                             idempotency_key="latency-query:" + uuid4().hex)
        if result["status"] != "succeeded":
            raise AssertionError(result)
        return {"querySeconds": time.monotonic() - started}

    def _drive(self, case):
        interaction_id = case["input_interaction"]
        for _ in range(5):
            result = self.fixture.resume(interaction_id)
            interaction = result.get("interaction")
            if interaction and interaction["resume"]["ready"]:
                interaction_id = interaction["interactionId"]
            else:
                return result
        raise AssertionError("Original coordinator did not settle")

    def process(self, scenario):
        offset = self.fixture.latency.snapshot()
        started = time.monotonic()
        if scenario == "query":
            result = self.query()
            return {**result, **self.fixture.latency.since(offset), "totalSeconds": time.monotonic() - started}
        before_writes = len(self.fixture.oa.writes)
        if scenario in {"mixed", "legacy"}:
            case = self.fixture.add_pending_batch(("missed_punch", "missed_punch") if scenario == "legacy"
                                                  else ("missed_punch", "weekly_report"), legacy=scenario == "legacy")
            count = 2
        else:
            case = self.fixture.add_pending_case("attendance_confirmation")
            count = 1
        timings = {"prepareSeconds": time.monotonic() - started}
        receipts = []
        for ordinal in range(count):
            path = f"/flow/input/{case['input_id']}"
            status, page, elapsed = self.cards.request("GET", path)
            if status != 200:
                raise AssertionError((status, page))
            timings["cardGetSeconds"] = timings.get("cardGetSeconds", 0) + elapsed
            config = json.loads(re.search(r"mountBusinessDraft\((\{[^\n]+\}), document, window\);", page)[1])
            token = config["csrfToken"]
            status, saved, elapsed = self.cards.json("POST", path + "/draft/values",
                {"expectedRevision": config["state"]["revision"], "values": {"opinion": "隔离性能验收意见。"}}, draft_token=token)
            if status != 200:
                raise AssertionError((status, saved))
            timings["saveSeconds"] = timings.get("saveSeconds", 0) + elapsed
            status, preview, elapsed = self.cards.json("POST", path + "/draft/preview",
                {"expectedRevision": saved["revision"]}, draft_token=token)
            if status != 200:
                raise AssertionError((status, preview))
            timings["previewSeconds"] = timings.get("previewSeconds", 0) + elapsed
            decision_payload = {"expectedRevision": preview["revision"], "previewId": preview["preview"]["previewId"],
                                "requestKey": "http-latency-" + uuid4().hex}
            if scenario == "drift":
                affair = self.fixture.oa.data["affairs"][case["arguments"]["affair_id"]]
                affair["body_text"] += "新增需重新确认的合成材料。"
            status, final, elapsed = self.cards.json("POST", path + "/draft/approve", decision_payload, draft_token=token)
            timings["approveSeconds"] = timings.get("approveSeconds", 0) + elapsed
            if scenario == "drift":
                if status != 409 or final["error"]["code"] != "DRAFT_REVIEW_CHANGED":
                    raise AssertionError((status, final))
                if len(self.fixture.oa.writes) != before_writes:
                    raise AssertionError("Material drift crossed the write boundary")
                with sqlite3.connect(self.fixture.service.db_path) as connection:
                    if connection.execute("SELECT COUNT(*) FROM business_draft_decisions WHERE source_submission_id=?",
                                          (case["input_id"],)).fetchone()[0]:
                        raise AssertionError("Material drift unexpectedly created a final decision")
                return {**timings, **self.fixture.latency.since(offset), "totalSeconds": time.monotonic() - started,
                        "outcome": "material_change_blocked", "writes": 0}
            if status != 200:
                raise AssertionError((status, final))
            receipts.append(final["receipt"])
            # Identical request must observe the same durable decision.
            repeated_status, repeated, _ = self.cards.json("POST", path + "/draft/approve", decision_payload, draft_token=token)
            if repeated_status != 200 or repeated["receipt"] != final["receipt"]:
                raise AssertionError("Duplicate confirmation changed its decision")
            if scenario == "unknown":
                self.fixture.oa.arm("approval_reply_lost")
            resume_started = time.monotonic()
            result = self._drive(case)
            timings["resumeSeconds"] = timings.get("resumeSeconds", 0) + time.monotonic() - resume_started
            status, projection, elapsed = self.workspace.json("GET", f"/api/tasks/{case['task_id']}")
            if status != 200:
                raise AssertionError((status, projection))
            timings["workspaceProjectionSeconds"] = timings.get("workspaceProjectionSeconds", 0) + elapsed
            if ordinal + 1 < count:
                case = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
                        "input_interaction": result["interaction"]["interactionId"]}
        after_writes = len(self.fixture.oa.writes)
        if scenario == "unknown":
            self.fixture.oa.arm(None)
            self._drive(case)
            if len(self.fixture.oa.writes) != after_writes:
                raise AssertionError("Unknown outcome replayed a write")
        elif after_writes - before_writes != count:
            raise AssertionError("Expected exact one downstream effect per item")
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            for receipt in receipts:
                rows = connection.execute("SELECT COUNT(*) FROM business_draft_decisions WHERE authorization_id=?",
                                          (receipt["authorizationId"],)).fetchone()[0]
                if rows != 1:
                    raise AssertionError("Expected one exact durable decision")
                authorization = self.fixture.service.write_authorizations.get(receipt["authorizationId"])
                operation = self.fixture.service.operations.get(authorization["commit_operation_id"])
                expected_status = "unknown" if scenario == "unknown" else "succeeded"
                if operation["status"] != expected_status or authorization["state"] != "consumed":
                    raise AssertionError("Unexpected commit status or unconsumed authorization")
                if scenario != "unknown" and operation["result"]["verification"].get("confirmed") is not True:
                    raise AssertionError("Successful result lacks authoritative verification")
        return {**timings, **self.fixture.latency.since(offset), "totalSeconds": time.monotonic() - started,
                "outcome": "unknown_no_replay" if scenario == "unknown" else "succeeded", "writes": after_writes - before_writes}


def summarize(samples):
    groups = {}
    for sample in samples:
        group = groups.setdefault(sample["scenario"], {"samples": 0, "errors": 0, "metrics": {},
                                                      "downstreamCounts": {}, "controlledDelaySeconds": {}})
        group["samples"] += 1
        if "error" in sample:
            group["errors"] += 1
        for key, value in sample.items():
            if key.endswith("Seconds") and isinstance(value, (int, float)):
                group["metrics"].setdefault(key, []).append(value)
        for key, value in sample.get("counts", {}).items():
            group["downstreamCounts"].setdefault(key, []).append(value)
        for key, value in sample.get("controlledDelaySeconds", {}).items():
            group["controlledDelaySeconds"].setdefault(key, []).append(value)
    for group in groups.values():
        for kind in ("metrics", "downstreamCounts", "controlledDelaySeconds"):
            group[kind] = {key: distribution(values) for key, values in group[kind].items()}
    return groups


def compare_reports(baseline, candidate):
    """Fail closed if the two workloads or evidence boundaries differ."""
    for name in ("benchmarkContract", "latencyContract", "measurementKind"):
        if baseline["metadata"][name] != candidate["metadata"][name]:
            raise ValueError(f"Not comparable: {name} differs")
    rows = {}
    for scenario, after in candidate["summary"].items():
        before = baseline["summary"].get(scenario)
        if before is None or before["samples"] != after["samples"]:
            raise ValueError(f"Not comparable: {scenario} sample count differs")
        if before["errors"] or after["errors"]:
            raise ValueError(f"Not comparable: {scenario} contains failed samples")
        rows[scenario] = {}
        for name, metric in after["metrics"].items():
            old = before["metrics"].get(name)
            if old is None:
                continue
            rows[scenario][name] = {"baselineMedian": old["median"], "candidateMedian": metric["median"],
                                    "baselineP95": old["p95"], "candidateP95": metric["p95"],
                                    "medianChangePercent": (metric["median"] / old["median"] - 1) * 100 if old["median"] else None}
    return rows

