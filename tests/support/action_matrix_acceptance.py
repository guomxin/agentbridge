"""Isolated Workspace action matrix with explicitly different evidence boundaries.

Taihua, room application and four Smartlight actions run real prepare/commit and
readback against synthetic HTTP. Eight other OA actions use narrow synthetic
prepare/commit bindings with original schemas, normalizers and summaries. They
exercise the real Central/Draft/authorization/coordinator, not the OA DOM adapter.
No worker has an external-network implementation.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from unittest.mock import patch

from agentbridge.adapters import seeyon_business_trip as trip
from agentbridge.adapters import seeyon_business_trip_submit as trip_submit
from agentbridge.adapters import seeyon_leave as leave
from agentbridge.adapters import seeyon_leave_submit as leave_submit
from agentbridge.adapters import seeyon_missed_punch as missed
from agentbridge.adapters import seeyon_meeting as meeting
from agentbridge.adapters import seeyon_meeting_room_application as room
from agentbridge.adapters import seeyon_workflow_revoke as revoke
from agentbridge.adapters.smartlight import SmartlightCentralAdapter
from agentbridge.core.business_draft_profiles import BY_PREPARE
from agentbridge.core import write_catalog
from tests.support.business_draft_acceptance import HTTP_ORIGIN, NetworkFreeHttpWorker, OWNER
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, FullOaTransport, FullOaWorker, FullOaPage
from tests.support.interaction_acceptance import write_metadata
from tests.test_smartlight_adapter import FakeSmartlightWorker

SYNTHETIC_BINDINGS = {
    "oa.business_trip.prepare": (trip.normalize_business_trip_inputs, trip.business_trip_summary),
    "oa.business_trip.submit.prepare": (trip.normalize_business_trip_inputs, trip_submit.business_trip_submit_summary),
    "oa.leave.prepare": (leave.normalize_leave_inputs, leave.leave_summary),
    "oa.leave.submit.prepare": (leave.normalize_leave_inputs, leave_submit.leave_submit_summary),
    "oa.missed_punch.prepare": (missed.normalize_missed_punch_inputs, missed.missed_punch_draft_summary),
    "oa.meeting.create.prepare": (meeting.normalize_meeting_inputs, meeting.meeting_summary),
    "oa.meeting_room.application.cancel.prepare": (room.normalize_meeting_room_application_cancel_inputs, room.meeting_room_application_cancel_summary),
    "oa.workflow.revoke.prepare": (revoke.normalize_workflow_revoke_inputs, revoke.workflow_revoke_summary),
}
FIELDLESS = (
    "smartlight.alarm.work_area.submit.prepare", "smartlight.alarm.work_area.revoke.prepare",
    "smartlight.alarm.dispose.prepare",
)
MATRIX_CAPABILITIES = (*SYNTHETIC_BINDINGS, "oa.meeting_room.application.prepare",
                       "taihua.work_log.create.prepare", "smartlight.alarm.remark.update.prepare", *FIELDLESS)
SYNTHETIC_EVIDENCE = "Real Central/schema/normalizer/draft/authorization/coordinator; synthetic OA prepare/commit boundary; not OA DOM adapter validation"
HTTP_EVIDENCE = "Real adapter prepare/commit/readback and Central; synthetic downstream HTTP only"


class MatrixOaTransport(FullOaTransport):
    def request(self, method, url, *, body=None, **kwargs):
        self.validate_url(url, {"/seeyon/ajax.do", "/seeyon/rest/template/myTemplate"})
        fields = parse_qs(body or "")
        if method == "POST" and fields.get("managerMethod") == ["meetingInfo"]:
            payload = {"id_temp": "synthetic-meeting-content", "currentUser": {"id": "synthetic-user", "name": OWNER}}
            return {"status": 200, "url": url, "json": payload, "text": json.dumps(payload)}
        return super().request(method, url, body=body, **kwargs)


class MatrixOaPage(FullOaPage):
    """Expose only the registered sent-list/precheck IO used by content identity.

    The production identity parser and eligibility checks remain intact. This
    does not replace the matrix's explicitly synthetic prepare/commit boundary.
    """
    def wait_for_function(self, script, *, arg=None, **kwargs):
        if arg is None and "window.colManager" in script and "window.CallerResponder" in script:
            if parse_qs(urlparse(self.url).query).get("method") != ["listSent"]:
                raise AssertionError("Synthetic sent-grid readiness requires its exact page")
            return
        return super().wait_for_function(script, arg=arg, **kwargs)

    def evaluate(self, script, argument=None):
        if isinstance(argument, dict) and "manager_method" in argument:
            if (argument != {"manager_method": "getSentList", "page_size": 100, "max_pages": 20}
                    or parse_qs(urlparse(self.url).query).get("method") != ["listSent"]):
                raise AssertionError("Synthetic matrix rejects unknown collection IO")
            self.worker.transport.data["calls"].append({"method": "DOM", "action": "getSentList"})
            self.worker.transport.persist()
            return deepcopy(self.worker.transport.data.get("sent", []))
        if isinstance(argument, dict) and set(argument) == {"affair_id", "summary_id"}:
            matches = [row for row in self.worker.transport.data.get("sent", [])
                       if row["affairId"] == argument["affair_id"] and row["summaryId"] == argument["summary_id"]]
            if (len(matches) != 1 or "checkIsCanRepeal" not in script
                    or parse_qs(urlparse(self.url).query).get("method") != ["listSent"]):
                raise AssertionError("Synthetic matrix rejects mismatched revoke precheck")
            self.worker.transport.data["calls"].append({"method": "DOM", "action": "revokeEligibility"})
            self.worker.transport.persist()
            return deepcopy(self.worker.transport.data["revoke_eligibility"][argument["affair_id"]])
        return super().evaluate(script, argument)


class MatrixOaWorker(FullOaWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = MatrixOaPage(self)


class MatrixSmartlightTransport:
    """Exact allowlisted synthetic HTTP routes; actual Smartlight adapter is intact."""
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self.data = json.loads(path.read_text()) if path.exists() else {"alarms": {}, "remarks": {}, "writes": [], "fault": None}

    def persist(self):
        write_metadata(self.path, self.data)

    def add_alarm(self, alarm_id, *, submitted=False):
        record = deepcopy(FakeSmartlightWorker().alarm_record)
        record.update(hitchAlarmId=alarm_id, rtuName="合成验收控制柜", workAreaName="合成验收工区", isSubmitWorkArea=int(submitted))
        self.data["alarms"][alarm_id] = record
        self.persist()

    def request(self, method, url, *, body=None, **kwargs):
        parsed = urlparse(url)
        if method != "POST" or f"{parsed.scheme}://{parsed.netloc}" != HTTP_ORIGIN:
            raise AssertionError("Synthetic Smartlight rejects external origin or method")
        fields = parse_qs(body or "", keep_blank_values=True)
        name = parsed.path.rsplit("/", 1)[-1]
        with self.lock:
            if parsed.path == "/jwtcenter//JWTInfoController/refreshToken":
                payload = {"resp_code": 1000, "resp_data": {"access_token": "synthetic-access", "access_token_duration": 3600}}
            elif not parsed.path.startswith("/smartlight/rHisHitchAlarm/"):
                raise AssertionError("Synthetic Smartlight rejects unlisted route")
            elif name == "getDataByRtuAlarm":
                payload = {"RtuHisHitchAlarm": {"list": deepcopy(list(self.data["alarms"].values())), "totalCount": len(self.data["alarms"])}}
            elif name == "getRtuAlarmRemark":
                payload = deepcopy(self.data["remarks"].get(fields["hitchAlarmId"][0]))
            elif name in {"saveRtuAlarmRemark", "updateIsSubmitWorkArea", "cancleSubmitWorkArea", "setRtuConductStatusDisposed"}:
                if name == "saveRtuAlarmRemark":
                    value = json.loads(fields["json"][0])
                    alarm_id = value["hitchAlarmId"]
                    self.data["remarks"][alarm_id] = value
                    payload = {"code": 200, "message": "合成保存成功"}
                else:
                    alarm_id = fields["json" if name == "setRtuConductStatusDisposed" else "hitchAlarmIds"][0]
                    record = self.data["alarms"][alarm_id]
                    record["conductStatue" if name == "setRtuConductStatusDisposed" else "isSubmitWorkArea"] = (
                        3 if name == "setRtuConductStatusDisposed" else int(name == "updateIsSubmitWorkArea"))
                    payload = 1
                self.data["writes"].append({"action": name, "alarm_id": alarm_id})
                self.persist()
                if self.data["fault"] == "lost_reply":
                    raise ConnectionError("Synthetic response lost after one effect")
            else:
                raise AssertionError("Synthetic Smartlight rejects unlisted action")
            return {"status": 200, "url": url, "json": payload, "text": "", "content_type": "application/json"}


class ActionMatrixAcceptanceFixture(FullBusinessAcceptanceFixture):
    def __init__(self, home: Path, *, seed=True):
        super().__init__(home, seed=False)
        self.patches = ExitStack()
        self.oa = MatrixOaTransport(self.home / "synthetic-oa-views.json")
        self.service._worker_factories_by_system["oa"] = lambda *_: MatrixOaWorker(self.oa)
        self.matrix_path = self.home / "synthetic-action-matrix.json"
        self.matrix = json.loads(self.matrix_path.read_text()) if self.matrix_path.exists() else {"effects": [], "fault": None}
        self.smartlight = MatrixSmartlightTransport(self.home / "synthetic-smartlight.json")
        self.service._adapters_by_system["smartlight"] = SmartlightCentralAdapter(base_url=HTTP_ORIGIN + "/smartlight/", allow_insecure_http=True)
        self.service._adapter_systems["smartlight-central"] = "smartlight"
        self.service._worker_factories_by_system["smartlight"] = lambda *_: NetworkFreeHttpWorker(self.smartlight)
        session = self.service.sessions.find(user_subject=OWNER, system_id="smartlight")
        if not session or session["state"] != "active":
            session = self.service.sessions.get_or_create(user_subject=OWNER, system_id="smartlight", expected_principal_ref=OWNER)
            session = self.service.sessions.activate(session["session_id"], observed_principal_ref=OWNER)
        self.service.session_states.save(session["session_id"], {"cookies": [], "http": {
            "access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
            "principal": {"account": OWNER, "name": OWNER, "organId": "synthetic-org", "organroleId": "synthetic-role", "userId": "synthetic-user"}}})
        for capability in SYNTHETIC_BINDINGS:
            definition = write_catalog._TRUSTED_WRITE_DEFINITIONS[capability]
            self.patches.enter_context(patch.object(write_catalog, definition["prepare_function"], self._prepare_binding(capability)))
            self.patches.enter_context(patch.object(write_catalog, definition["commit_function"], self._commit_binding(capability)))
        self.meta.setdefault("matrix_cases", [])
        if seed and not self.meta["matrix_cases"]:
            for capability in MATRIX_CAPABILITIES:
                self.add_matrix_case(capability)
        write_metadata(self.meta_path, self.meta)

    def _prepare_binding(self, capability):
        def prepare(_adapter, _worker, arguments):
            normalizer, summarize = SYNTHETIC_BINDINGS[capability]
            values = normalizer(arguments)
            profile = BY_PREPARE[capability]
            target = {"template_id": "synthetic-template-" + profile.review_kind, "form_app_id": "synthetic-form-" + profile.review_kind}
            if capability == "oa.meeting.create.prepare":
                selected = next(item for item in self.oa.data["rooms"] if values["room"] in {item["roomId"], item["roomName"]})
                target = {"room_id": selected["roomId"], "room_name": selected["roomName"]}
                summary = summarize(values, selected["roomName"])
            elif capability == "oa.meeting_room.application.cancel.prepare":
                target = {"application_id": values["application_id"], "room_id": "synthetic-cancel-room", "room_name": "合成待撤销会议室", "start_time": "2026-10-21 14:00", "end_time": "2026-10-21 15:00"}
                summary = summarize(target, values["cancellation_reason"])
            elif capability == "oa.workflow.revoke.prepare":
                target = {"affair_id": values["affair_id"], "summary_id": "synthetic-summary", "process_id": "synthetic-process", "title": "合成已发流程", "create_date": "2026-10-15", "current_nodes": "合成处理人"}
                summary = summarize(target, values["repeal_comment"])
            else:
                summary = summarize(values)
            return {"plan": {"schema_version": "synthetic.action_matrix.v1", "business_intent": profile.action_kind,
                "synthetic_capability": capability, "target": target, "exact_input": values,
                "form_contract": {"version": "synthetic-boundary-v1"}, "expected_effect": {"action": profile.action_kind}},
                "summary": {**summary, "acceptance_evidence_boundary": SYNTHETIC_EVIDENCE}}
        return prepare

    def _commit_binding(self, capability):
        def commit(_adapter, _worker, plan, *, enter_commit_boundary, **_kwargs):
            if plan.get("synthetic_capability") != capability:
                raise AssertionError("Synthetic frozen action mismatch")
            normalizer, _ = SYNTHETIC_BINDINGS[capability]
            normalizer(plan["exact_input"])
            enter_commit_boundary()
            with self.lock:
                record = {"record_id": uuid4().hex, "capability": capability, "values": plan["exact_input"]}
                self.matrix["effects"].append(record)
                write_metadata(self.matrix_path, self.matrix)
                if self.matrix["fault"] == "lost_reply":
                    raise write_catalog._TRUSTED_WRITE_DEFINITIONS[capability]["outcome_error"]("Synthetic response lost after effect")
                readback = json.loads(self.matrix_path.read_text())["effects"][-1]
                assert readback == record
            return {"schema_version": "synthetic.action_matrix_result.v1", "business_intent": plan["business_intent"],
                "effect": BY_PREPARE[capability].effect, "record_id": record["record_id"],
                "verification": {"confirmed": True, "method": "synthetic_persistent_ledger_readback"},
                "acceptance_evidence_boundary": SYNTHETIC_EVIDENCE}
        return commit

    def add_matrix_case(self, capability):
        if capability not in MATRIX_CAPABILITIES:
            raise ValueError("Not in the closed action matrix")
        key = uuid4().hex[:8]
        arguments = {}
        if capability in {"oa.meeting.create.prepare", "oa.meeting_room.application.prepare"}:
            arguments = {"start_time": "2026-10-21 10:00", "end_time": "2026-10-21 11:00"}
        elif capability == "oa.meeting_room.application.cancel.prepare":
            arguments = {"application_id": "synthetic-cancel-" + key}
            # Source-time private-content identity now reads My Applications.
            # Supply the raw response for the actual parser/eligibility checks.
            self.oa.data["applications"].append({"roomAppId": arguments["application_id"],
                "roomId": "synthetic-cancel-room", "roomName": "合成待撤销会议室", "meetingId": "", "meetingName": "",
                "description": "合成独立申请", "startDatetime": 1792562400000, "endDatetime": 1792566000000,
                "appStatus": 0, "appStatusName": "待审核", "usedStatusDisplay": 0})
            self.oa.persist()
        elif capability == "oa.workflow.revoke.prepare":
            arguments = {"affair_id": "synthetic-revoke-" + key}
            self.oa.data.setdefault("sent", []).append({"affairId": arguments["affair_id"],
                "summaryId": "synthetic-summary", "processId": "synthetic-process", "subject": "合成已发流程",
                "createDate": "2026-10-15", "currentNodesInfo": "合成处理人", "bodyType": "20",
                "templeteId": "synthetic-revoke-template", "formAppId": "synthetic-revoke-form",
                "formRecordid": "synthetic-revoke-record-" + key, "flowFinished": False,
                "state": 0, "subState": 0, "subStateName": "", "summaryState": 0, "affairState": 2})
            self.oa.data.setdefault("revoke_eligibility", {})[arguments["affair_id"]] = {
                "ready": True, "secret_message": "", "repeal_message": "", "affair_message": ""}
            self.oa.persist()
        elif capability.startswith("smartlight."):
            arguments = {"alarm_id": "synthetic-alarm-" + key}
            self.smartlight.add_alarm(arguments["alarm_id"], submitted=capability == FIELDLESS[1])
        title = (BY_PREPARE[capability].title if capability in BY_PREPARE else
                 {FIELDLESS[0]: "提交告警到工区", FIELDLESS[1]: "撤回工区提交", FIELDLESS[2]: "不可逆处置告警"}[capability])
        evidence = SYNTHETIC_EVIDENCE if capability in SYNTHETIC_BINDINGS else HTTP_EVIDENCE
        task_id = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key="action-matrix|" + key, endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"], title=f"动作矩阵 · {title} · {key}")["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key="action-matrix:" + key, role="assistant", text="隔离动作矩阵验收：" + title + "。证据边界：" + evidence, task_id=task_id)
        result = self.service.invoke(user_subject=OWNER, capability_name=capability, arguments=arguments, task_id=task_id, idempotency_key="matrix-source:" + key)
        if result["status"] != "requires_user_action":
            raise RuntimeError(f"Matrix source creation failed: {capability}: {result}")
        action = result["nextAction"]
        case = {"key": key, "profile": capability, "capability": capability, "task_id": task_id,
                "input_interaction": result["interaction"]["interactionId"], "arguments": arguments, "evidence_boundary": evidence}
        if capability in FIELDLESS:
            case["authorization_id"] = action["authorizationId"]
            case["flow_url"] = f"{self.card_url}/flow/authorize/{case['authorization_id']}"
        else:
            case["input_id"] = action["inputSubmissionId"]
            case["flow_url"] = f"{self.card_url}/flow/input/{case['input_id']}"
        self.meta["matrix_cases"].append(case)
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def public_metadata(self):
        return {"fixture": "action-matrix-explicit-boundaries-v1", "workspace_url": self.workspace_url,
                "card_origin": self.card_url, "username": "cardreview", "cases": self.meta["matrix_cases"],
                "synthetic_boundary_effects": len(self.matrix["effects"]), "smartlight_effects": len(self.smartlight.data["writes"]),
                "taihua_effects": self.transport.write_calls, "oa_http_effects": len(self.oa.writes)}

    def close(self):
        super().close()
        self.patches.close()
