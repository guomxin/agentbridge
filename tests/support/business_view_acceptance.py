"""Real OA drafts and coordinator, backed only by synthetic HTTP/DOM workers.

The Seeyon adapter, HTML parsers, preflight, prepare, commit and authoritative
readback are unchanged. The worker implements no external network operation.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4
from unittest.mock import patch

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from tests.authorization_fixtures import grant_permissions
from tests.support.business_draft_acceptance import (
    BusinessDraftAcceptanceFixture, HTTP_ORIGIN, NetworkFreeHttpWorker, OWNER,
)
from tests.support.interaction_acceptance import write_metadata
from tests.test_seeyon_pending_actions import FakeBusinessFrame, _fixture

ROOM_PREPARE = "oa.meeting_room.application.prepare"
LEAVE_PREPARE = "oa.leave.approval.prepare"
LEGACY_PREPARE = "oa.overtime.approval.prepare"
ROOM_VALUES = {"purpose": "合成会议室单次确认验收", "room": "",
               "start_time": "2026-10-20 10:00", "end_time": "2026-10-20 11:00"}


def _ms(value):
    local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
    return int(local.timestamp() * 1000)


class SyntheticOaTransport:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.RLock()
        self.data = json.loads(path.read_text()) if path.exists() else {
            "rooms": [{"roomId": "room-a", "roomName": "合成甲会议室", "roomTypeId": "type-1"},
                      {"roomId": "room-b", "roomName": "合成乙会议室", "roomTypeId": "type-1"}],
            "bookings": [{"roomId": "room-a", "appBeginDate": _ms("2026-10-20 13:00"),
                          "appEndDate": _ms("2026-10-20 18:00")}],
            "applications": [], "affairs": {}, "calls": [], "fault": None,
        }
        self.workers = []

    def persist(self):
        write_metadata(self.path, self.data)

    def apply_control(self):
        """A local operator can change synthetic evidence while Chrome stays open."""
        path = self.path.parent / "business-view-control.json"
        if not path.exists():
            return
        control = json.loads(path.read_text())
        if control["id"] == self.data.get("last_control"):
            return
        if control["action"] == "change_material":
            self.change_material(control["affair_id"])
        elif control["action"] == "fault":
            self.arm(control["fault"])
        else:
            raise ValueError("Unknown local synthetic acceptance control")
        self.data["last_control"] = control["id"]
        self.persist()

    def arm(self, fault):
        if fault not in {None, "room_readback_missing", "approval_reply_lost", "pending_partial", "done_identity_changed"}:
            raise ValueError("Unknown synthetic OA fault")
        self.data["fault"] = fault
        self.persist()

    def add_affair(self, affair_id, *, profile="leave"):
        fixture = deepcopy(_fixture(profile))
        fixture["source"].update(affair_id=affair_id, sender="合成申请人", date="2026-10-13")
        fixture["source"]["title"] = "【HR】请假申请单-合成申请人-年休" if profile == "leave" else "【HR】加班申请审核单-合成申请人"
        fixture["detail"]["title"] = fixture["source"]["title"]
        if profile == "leave":
            fixture["business_snapshot"].update(applicant="合成申请人", employee_number="SYNTHETIC-001",
                department="合成验收部门", application_date="2026-10-13", reason="合成请假事由",
                start_time="2026-10-21 08:30", end_time="2026-10-23 17:30")
            fixture["detail"]["fields"] = [
                {"name": "姓名", "value": "合成申请人 SYNTHETIC-001 合成验收部门 发起日期 2026-10-13"},
                {"name": "请假类型", "value": "年休 剩余年休 5.88天 剩余调休 61.41小时"},
                {"name": "开始时间", "value": "2026-10-21 08:30 结束时间 2026-10-23 17:30 3.00天 22.50小时"},
                {"name": "事由", "value": "合成请假事由"},
                {"name": "证明材料", "value": "合成材料说明.txt"},
                {"name": "是否存在有非部门经理的直接上级（组负责人）", "value": "否"},
            ]
        fixture["detail"]["attachments"] = [{"name": "合成材料说明.txt"}]
        identity = fixture["signals"]["identity"]
        identity.update(summary_id=f"summary-{affair_id}", process_id=f"process-{affair_id}", form_record_id=f"record-{affair_id}")
        fixture["processed"] = False
        self.data["affairs"][affair_id] = fixture
        self.persist()

    def change_material(self, affair_id):
        fixture = self.data["affairs"][affair_id]
        fixture["business_snapshot"]["reason"] = "材料已经变更，需要重新核对"
        next(item for item in fixture["detail"]["fields"] if item["name"] == "事由")["value"] = fixture["business_snapshot"]["reason"]
        fixture["detail"]["attachments"] = [{"name": "更新后的合成材料.txt"}]
        self.persist()

    def validate_url(self, url, paths):
        parsed = urlparse(url)
        if f"{parsed.scheme}://{parsed.netloc}" != HTTP_ORIGIN or parsed.path not in paths:
            raise AssertionError("Synthetic OA worker rejects this origin/path")
        return parsed

    def request(self, method, url, *, body=None, **_kwargs):
        parsed = self.validate_url(url, {"/seeyon/ajax.do", "/seeyon/rest/template/myTemplate"})
        query = parse_qs(parsed.query)
        with self.lock:
            self.apply_control()
            if method == "GET" and parsed.path.endswith("/rest/template/myTemplate"):
                payload = {"code": 0, "data": {"templates": []}}
                action = "templates"
            elif method == "GET" and query.get("managerName") == ["sectionManager"] and query.get("managerMethod") == ["doProjection"]:
                arguments = json.loads(query["arguments"][0])
                if arguments.get("sectionBeanId") != "pendingSection":
                    raise AssertionError("Only synthetic pending section reads are allowed")
                rows = []
                for affair_id, fixture in self.data["affairs"].items():
                    if fixture["processed"]:
                        continue
                    source = fixture["source"]
                    rows.append({"cells": [
                        {"cellContentHTML": escape(source["title"]), "id": affair_id,
                         "linkURL": f"/seeyon/collaboration/collaboration.do?method=summary&affairId={affair_id}"},
                        {"cellContentHTML": source["sender"]}, {"cellContentHTML": source["date"]},
                        {"cellContentHTML": "审批"},
                    ]})
                total = len(rows) + (1 if self.data["fault"] == "pending_partial" else 0)
                payload = {"Name": "pendingSection", "Data": {"rows": rows, "dataCount": total, "pageNo": 1}}
                action = "pending"
            elif method == "POST" and parsed.path == "/seeyon/ajax.do" and query.get("managerName") == ["meetingAjaxManager"]:
                fields = parse_qs(body or "")
                action, arguments = fields["managerMethod"][0], json.loads(fields["arguments"][0])
                if action == "roomListInfo":
                    payload = {"roomsInfo": deepcopy(self.data["rooms"]), "roomAppsInfo": deepcopy(self.data["bookings"])}
                elif action == "validateRoomApps":
                    payload = {"success": True, "data": []}
                elif action == "getMyApps":
                    rows = [] if self.data["fault"] == "room_readback_missing" else deepcopy(self.data["applications"])
                    payload = {"data": rows, "total": len(rows), "pages": 1}
                elif action == "applyRoom":
                    app = arguments[0]["roomApps"][0]
                    self.data["applications"].append({"roomAppId": f"synthetic-{uuid4().hex}",
                        "roomId": app["roomId"], "roomName": app["roomName"], "meetingId": "", "meetingName": "",
                        "description": app["description"], "startDatetime": app["appBeginDate"], "endDatetime": app["appEndDate"],
                        "appStatus": 0, "appStatusName": "待审核", "usedStatusDisplay": 0})
                    self.data["bookings"].append({"roomId": app["roomId"], "appBeginDate": app["appBeginDate"], "appEndDate": app["appEndDate"]})
                    payload = {"success": True}
                else:
                    raise AssertionError(f"Synthetic OA rejects managerMethod {action}")
            else:
                raise AssertionError("Synthetic OA rejects HTTP method/route")
            self.data["calls"].append({"method": method, "action": action})
            self.persist()
            return {"status": 200, "url": url, "json": payload, "text": json.dumps(payload)}

    def submit(self, payload, worker):
        affair_id = payload["affair_id"]
        if worker.is_reader or worker.affair_id != affair_id or self.data["affairs"][affair_id]["processed"]:
            raise AssertionError("Submission worker/target is invalid or already processed")
        with self.lock:
            self.apply_control()
            self.data["calls"].append({"method": "DOM", "action": "submit", "affair_id": affair_id,
                                       "opinion": payload["opinion"]})
            self.data["affairs"][affair_id]["processed"] = True
            self.persist()
        if self.data["fault"] == "approval_reply_lost":
            raise ConnectionError("Synthetic submit acknowledgement lost after the one effect")
        return {"scheduled": True, "submit_entry": "submitClickFunc"}

    @property
    def writes(self):
        return [call for call in self.data["calls"] if call["action"] in {"applyRoom", "submit"}]


class SyntheticOaPage:
    def __init__(self, worker):
        self.worker, self.handlers, self.pumped = worker, {}, []
        self.url = f"{HTTP_ORIGIN}/seeyon/"

    @property
    def frames(self):
        return [FakeBusinessFrame(self.worker.fixture)] if self.worker.affair_id else []

    def wait_for_function(self, _script, *, arg, **_kwargs):
        if arg != {"gridId": "listDone", "managerMethod": "getDoneList"}:
            raise AssertionError(f"Unexpected synthetic grid contract: {arg}")

    def evaluate(self, _script, argument=None):
        if argument is None:
            return []  # Passive submission tracker; no injected dialogs/errors.
        if isinstance(argument, str):
            if argument != self.worker.affair_id:
                raise AssertionError("Synthetic DOM affair mismatch")
            signals = deepcopy(self.worker.fixture["signals"])
            if self.worker.is_reader and self.worker.transport.data["fault"] == "done_identity_changed":
                signals["identity"]["process_id"] = "different-process"
            return signals
        if set(argument) >= {"affair_id", "opinion", "attitude_code"}:
            return self.worker.transport.submit(argument, self.worker)
        if set(argument) == {"gridId", "dateFields"}:
            if not self.worker.is_reader:
                raise AssertionError("Readback must use an independent synthetic page")
            rows = [{**deepcopy(fixture["source"]), "date_basis": "processed_at"}
                    for fixture in self.worker.transport.data["affairs"].values() if fixture["processed"]]
            return {"items": rows, "total": len(rows), "page": 1}
        raise AssertionError("Synthetic DOM rejects unknown evaluate contract")

    def on(self, event, callback):
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        if self.handlers.get(event) is callback:
            del self.handlers[event]

    def wait_for_timeout(self, milliseconds):
        self.pumped.append(milliseconds)


class SyntheticOaWorker(NetworkFreeHttpWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport)
        self.is_reader, self.affair_id = is_reader, None
        self.page = SyntheticOaPage(self)
        self.readback_opened = self.readback_closed = False
        transport.workers.append(self)

    @property
    def fixture(self):
        return self.transport.data["affairs"][self.affair_id]

    @property
    def page_url(self):
        return self.page.url

    @property
    def page_title(self):
        return f"致远 OA, {self.transport.data.get('principal', OWNER)}, 您好"

    def goto(self, url, **_kwargs):
        self.transport.validate_url(url, {"/seeyon/", "/seeyon/collaboration/collaboration.do"})
        self.page.url = url
        return self.page

    def resource_urls(self):
        return [f"{HTTP_ORIGIN}/seeyon/ajax.do?" + urlencode({"managerName": "sectionManager", "managerMethod": "doProjection",
            "arguments": json.dumps({"sectionBeanId": "pendingSection"})})]

    def rendered_snapshot(self, url, **_kwargs):
        parsed = self.transport.validate_url(url, {"/seeyon/collaboration/collaboration.do"})
        self.affair_id = parse_qs(parsed.query)["affairId"][0]
        detail = self.fixture["detail"]
        self.page.url = url
        rows = "".join(f"<tr><th>{escape(item['name'])}</th><td>{escape(item['value'])}</td></tr>" for item in detail["fields"])
        attachments = "".join(f"<a href='/seeyon/fileUpload.do?method=download&amp;fileId=synthetic-{index}'>{escape(item['name'])}</a>"
                              for index, item in enumerate(detail["attachments"]))
        html = f"<html><body><h1>{escape(detail['title'])}</h1><table>{rows}</table>{attachments}"
        html += "<div class='processLog'>合成申请人 发起 2026-10-13</div><script>var jsonArrBase = '[{\"codes\":[\"ContinueSubmit\"]}]';</script></body></html>"
        # Every synthetic form frame is also present in the rendered capture;
        # real material completeness checks must not be bypassed by the fixture.
        return {"url": url, "html": "<html><body></body></html>",
                "frames": [{"url": frame.url, "html": html} for frame in self.page.frames]}

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield SyntheticOaWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class BusinessViewAcceptanceFixture(BusinessDraftAcceptanceFixture):
    def __init__(self, home, *, seed=True):
        super().__init__(home, seed=False)
        self.oa = SyntheticOaTransport(self.home / "synthetic-oa-views.json")
        adapter = SeeyonCentralAdapter(base_url=f"{HTTP_ORIGIN}/seeyon/")
        self.service.adapter = adapter
        self.service._adapters_by_system["oa"] = adapter
        self.service._worker_factories_by_system["oa"] = lambda *_: SyntheticOaWorker(self.oa)
        if not self.meta.get("business_views_initialized"):
            existing = self.service.user_grants.get(OWNER) or {}
            grant_permissions(self.service.user_grants, OWNER, set(existing.get("permissions", [])) |
                {"oa.meeting_room.apply", "oa.meeting_room.read", "oa.leave.approve", "oa.overtime.approve"})
            self.meta["business_views_initialized"] = True
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        if not session or session["state"] != "active":
            session = self.service.sessions.get_or_create(user_subject=OWNER, system_id="oa", expected_principal_ref=OWNER)
            session = self.service.sessions.activate(session["session_id"], observed_principal_ref=OWNER)
        self.service.session_states.save(session["session_id"], {"cookies": []})
        self.meta.setdefault("view_cases", [])
        if seed and not self.meta["view_cases"]:
            self.add_view_cases()
        write_metadata(self.meta_path, self.meta)

    def add_view_cases(self):
        return [self.add_view_case(profile) for profile in ("meeting_room", "leave", "legacy_overtime")]

    def add_view_case(self, profile, *, case_key=None):
        case_key = case_key or uuid4().hex[:8]
        capability = {"meeting_room": ROOM_PREPARE, "leave": LEAVE_PREPARE, "legacy_overtime": LEGACY_PREPARE}[profile]
        arguments = {"start_time": ROOM_VALUES["start_time"], "end_time": ROOM_VALUES["end_time"]}
        if profile != "meeting_room":
            affair_id = f"synthetic-{profile}-{case_key}"
            self.oa.add_affair(affair_id, profile="leave" if profile == "leave" else "overtime")
            arguments = {"affair_id": affair_id}
        title = {"meeting_room": "会议室", "leave": "请假审批", "legacy_overtime": "旧入口加班审批"}[profile]
        task = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key=f"view-acceptance|{profile}|{case_key}", endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"],
            title=f"{title}业务视图验收 · {case_key}")["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key=f"view:{profile}:{case_key}", role="assistant", text=f"{title}独立验收。OA 下游仅为合成 HTTP/DOM，不连接真实系统。", task_id=task)
        # Construct a historical source through the original runtime path. The
        # capability now supports drafts, but old immutable sources stay legacy.
        with (patch("agentbridge.core.controlled_write_executor.profile_for_definition", return_value=None)
              if profile == "legacy_overtime" else nullcontext()):
            result = self.service.invoke(user_subject=OWNER, capability_name=capability, arguments=arguments,
                task_id=task, idempotency_key=f"view-source:{profile}:{case_key}")
        if result["status"] != "requires_user_action" or result["interaction"]["type"] != "business_input":
            raise RuntimeError(f"Actual OA prepare did not produce a business input: {result}")
        case = {"profile": profile, "key": case_key, "task_id": task, "input_id": result["nextAction"]["inputSubmissionId"],
                "input_interaction": result["interaction"]["interactionId"], "arguments": arguments}
        self.meta["view_cases"].append(case)
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def public_metadata(self):
        return {**super().public_metadata(), "fixture": "actual-business-views-synthetic-oa-v1",
            "cases": [{**case, "flow_url": f"{self.card_url}/flow/input/{case['input_id']}"} for case in self.meta["view_cases"][-3:]],
            "oa_write_count": len(self.oa.writes),
            "evidence_boundary": "Real central/drafts/Seeyon adapter/parsers/prepare/commit/readback; synthetic HTTP and DOM only"}
