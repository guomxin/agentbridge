"""All card profiles over real Central/Auth/Workspace and closed synthetic IO.

The 17 pending OA and meeting-reply adapters are real, including material
validation, authorization consumption and authoritative readback. The eight
historical ActionMatrix OA creation/cancellation bindings remain explicitly
synthetic at prepare/commit; this fixture does not claim their DOM coverage.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from html import escape
import json
import platform
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse
from uuid import uuid4
import zlib

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.core.business_draft_profiles import PROFILES
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.quick_queries import queue_query, execute_query
from tests.support.action_matrix_acceptance import (
    ActionMatrixAcceptanceFixture, MatrixOaTransport, MatrixOaWorker, MatrixOaPage,
    MATRIX_CAPABILITIES, FIELDLESS, SYNTHETIC_BINDINGS,
)
from tests.support.business_draft_acceptance import HTTP_ORIGIN, OWNER
from tests.support.card_content_acceptance import CardContentAcceptanceFixture
from tests.support.full_business_acceptance import FULL_REVIEW_PROFILES, _preview_png, _preview_pdf
from tests.support.interaction_acceptance import write_metadata
from tests.support.latency_action_matrix import MatrixHttpWorkload, OA_EVIDENCE
from tests.test_seeyon_meeting_reply import sample as meeting_sample
from tests.test_workspace import PASSWORD


CONTRACT = "card-material-real-central-closed-io-v1"
ALL_OA_PROFILES = (*FULL_REVIEW_PROFILES, "meeting_reply")
PROFILE_BY_REVIEW = {profile.review_kind: profile for profile in PROFILES if
    profile.view_kind == "approval" or profile.review_kind == "meeting_reply"}


def _pdf(objects):
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


def text_pdf(*, pages=24, long_page=False):
    """Predictable real PDF text: repeated search term and distinct page footer."""
    if not 1 <= pages <= 40:
        raise ValueError("Synthetic PDF page count is bounded")
    font = 3 + 2 * pages
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(pages))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()]
    height, lines = (1800, 85) if long_page else (792, 28)
    for index in range(pages):
        number = index + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 {height}] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * index} 0 R >>".encode())
        content = [f"BT /F1 11 Tf 35 {height - 35} Td (SYNTHETIC MATERIAL PAGE {number:02}) Tj"]
        content += [f"0 -18 Td (MATERIAL-CHECK page {number:02} line {line:02}: source text remains selectable.) Tj"
                    for line in range(1, lines + 1)]
        content += [f"0 -24 Td (UNIQUE-FOOTER-{number:02} END OF SOURCE PAGE {number:02}) Tj ET"]
        stream = "\n".join(content).encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _pdf(objects)


def scanned_pdf():
    """Image-only PDF: deliberately no font objects or text operators."""
    pixels = bytes(80 + ((x // 8 + y // 8) % 2) * 140 for y in range(120) for x in range(160))
    image_data = zlib.compress(pixels)
    stream = b"q 480 0 0 360 30 180 cm /Im0 Do Q"
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 540 720] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        f"<< /Type /XObject /Subtype /Image /Width 160 /Height 120 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length {len(image_data)} >>\nstream\n".encode() + image_data + b"\nendstream"])


class MaterialTransport(MatrixOaTransport):
    def __init__(self, path):
        super().__init__(path)
        self.data.setdefault("meetings", {})
        self.data.setdefault("material_io", [])

    def delay(self, kind, seconds, **details):
        seconds = float(seconds or 0)
        if not 0 <= seconds <= 8:
            raise AssertionError("Synthetic delay is outside its bounded contract")
        if not seconds:
            return
        started = time.monotonic()
        time.sleep(seconds)
        self.data["material_io"].append({"kind": kind, "requestedDelaySeconds": seconds,
            "actualDelaySeconds": time.monotonic() - started, **details})
        self.persist()

    def add_affair(self, affair_id, *, profile="leave"):
        if profile == "meeting_reply":
            if not str(affair_id).lstrip("-").isdigit():
                raise AssertionError("Meeting affair IDs use the actual numeric contract")
            meeting_id = str(abs(int(affair_id)) + 1)
            view = meeting_sample()
            view["meetingVo"].update(id=meeting_id, title="合成会议邀请 · " + meeting_id,
                content="<h2>本次会议议题</h2><p>仅回复本人的参会意向。</p><ul><li>合成材料核对</li><li>下一步安排</li></ul>")
            view["myReply"].update(id=str(abs(int(affair_id)) + 2), meetingId=meeting_id)
            self.data["meetings"][meeting_id] = {"affair_id": affair_id, "view": view, "fault": None}
            self.persist()
            return
        super().add_affair(affair_id, profile=profile)
        fixture = self.data["affairs"][affair_id]
        if profile == "efficiency_data":
            # This profile is a registered collaboration body, not a numeric
            # form contract. Its table and missing value come from actual HTML.
            # OA bodies can contain encoded HTML, the original Telegram
            # readability case. Preserve it as body text through the real
            # parser and safe-reading projection, not invented form fields.
            fixture["body_text"] += ('<section><h2>效能原始数据表</h2><p>统计期间：2026年10月（合成）；所属部门：合成验收部门。</p>'
                '<table><thead><tr><th>指标</th><th>本期</th><th>基准</th></tr></thead>'
                '<tbody><tr><td>合成处理量</td><td>12</td><td>未提供</td></tr><tr><td>合成耗时</td><td>3.50 小时</td><td>不适用</td></tr></tbody></table>'
                '<p>上述为原始正文表。材料未提供总金额、完成率或考核结论。</p></section>')
        elif profile == "quarterly_performance":
            from agentbridge.adapters.seeyon_quarterly_performance import ROW_FIELDS
            second = [dict(field, record_id="employee-2") for field in fixture["row_snapshot"]["fields"] if field["id"] in ROW_FIELDS]
            for field in second:
                if field["id"] == "field0006_id":
                    field["value"] = "合成员工乙"
                if field["id"] == "field0022_id":
                    field["value"] = "B"
            fixture["row_snapshot"]["fields"].extend(second)
        elif profile == "business_settlement":
            next(field for field in fixture["row_snapshot"]["fields"] if field["id"] == "field0028_id")["value"] = (
                "合成结算备注：收款账户、费用行和付款登记必须完整核对。" * 8)
        self.persist()

    def request(self, method, url, *, body=None, **kwargs):
        parsed = self.validate_url(url, {"/seeyon/ajax.do", "/seeyon/rest/template/myTemplate"})
        query = parse_qs(parsed.query)
        fields = parse_qs(body or "")
        name = fields.get("managerMethod", [None])[0]
        if method == "POST" and query.get("managerName") == ["meetingAjaxManager"] and name in {"meetingView", "reply"}:
            args = json.loads(fields["arguments"][0])
            if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], dict):
                raise AssertionError("Synthetic meeting requires one exact argument")
            value = args[0]
            meeting = self.data["meetings"].get(value.get("meetingId"))
            if meeting is None or value.get("proxyId") != "":
                raise AssertionError("Synthetic meeting target or proxy mismatch")
            if name == "meetingView":
                if set(value) != {"meetingId", "proxyId"}:
                    raise AssertionError("Unexpected meetingView contract")
                payload = deepcopy(meeting["view"])
            else:
                if (set(value) != {"meetingId", "proxyId", "feedbackFlag", "feedback", "atMembers"}
                        or value["feedbackFlag"] not in {"1", "0", "-1"} or value["atMembers"] != []
                        or meeting["view"]["myReply"]["feedbackFlag"] != -100):
                    raise AssertionError("Unexpected, mismatched or repeated meeting reply")
                meeting["view"]["myReply"].update(feedbackFlag=int(value["feedbackFlag"]), feedback=value["feedback"])
                payload = None
            self.data["calls"].append({"method": "POST", "action": name, "affair_id": meeting["affair_id"],
                **({"reply": deepcopy(value)} if name == "reply" else {})})
            self.persist()
            if name == "reply" and meeting["fault"] == "lost_receipt":
                raise ConnectionError("Synthetic reply effect persisted before receipt was lost")
            return {"status": 200, "url": url, "json": payload, "text": json.dumps(payload)}
        response = super().request(method, url, body=body, **kwargs)
        if method == "GET" and query.get("managerMethod") == ["doProjection"]:
            payload = response["json"]
            for meeting_id, meeting in self.data["meetings"].items():
                view = meeting["view"]["meetingVo"]
                payload["Data"]["rows"].append({"cells": [
                    {"cellContentHTML": escape(view["title"]), "id": meeting["affair_id"],
                     "linkURL": f"/seeyon/meeting.do?method=view&meetingId={meeting_id}"},
                    {"cellContentHTML": view["createUserName"]}, {"cellContentHTML": "2026-10-23"},
                    {"cellContentHTML": "会议"}]})
                payload["Data"]["dataCount"] += 1
            response["text"] = json.dumps(payload)
        return response

    def submit(self, payload, worker):
        result = super().submit(payload, worker)
        if self.data["affairs"][payload["affair_id"]].get("submission_fault") == "lost_receipt":
            raise ConnectionError("Synthetic OA effect persisted before submit receipt was lost")
        return result

    @property
    def writes(self):
        return [call for call in self.data["calls"] if call["action"] in {"applyRoom", "submit", "reply"}]


class MaterialPage(MatrixOaPage):
    def __init__(self, worker):
        super().__init__(worker)
        self.ready_started = None

    def evaluate(self, script, argument=None):
        if "agentbridge_pending_readiness" in script:
            hold = self.worker.fixture.get("ready_delay_seconds", 0)
            if hold:
                if self.ready_started is None:
                    self.ready_started = time.monotonic()
                ready = time.monotonic() - self.ready_started >= hold
                return {"identity_matches": True, "ready": ready, "body_loaded": ready,
                    "content_api_loaded": ready, "submit_entry": "submitClickFunc",
                    "reason": "ready" if ready else "body_loading"}
        return super().evaluate(script, argument)

    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        if self.worker.affair_id and self.worker.fixture.get("ready_delay_seconds"):
            self.worker.transport.delay("browser_wait", milliseconds / 1000, affair_id=self.worker.affair_id)


class MaterialWorker(MatrixOaWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = MaterialPage(self)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield MaterialWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class CardMaterialAcceptanceFixture(ActionMatrixAcceptanceFixture):
    def __init__(self, home, *, seed="browser"):
        if seed not in {"browser", "coverage", "empty"}:
            raise ValueError("Unknown card-material fixture seed")
        super().__init__(home, seed=False)
        self.oa = MaterialTransport(self.home / "synthetic-oa-views.json")
        self.service._worker_factories_by_system["oa"] = lambda *_: MaterialWorker(self.oa)
        self.credentials_path = self.home / "browser-credentials.json"
        write_metadata(self.credentials_path, {"username": "cardreview", "password": PASSWORD,
            "otherUsername": "cardother", "otherPassword": PASSWORD})
        self.flow = TrustedFlowApplication(service=self.service,
            field_application=TrustedFieldApplication(submission_store=self.service.field_submissions),
            action_application=TrustedActionApplication(authorization_store=self.service.write_authorizations),
            workspace_base_url=self.workspace_url)
        self.meta.setdefault("material_cases", [])
        self.meta.setdefault("material_batches", [])
        if self.meta.get("card_material_initialized"):
            if self.meta["card_material_seed"] != seed:
                raise ValueError("Reuse requires the same explicit material fixture seed")
            return
        self.meta["card_material_seed"] = seed
        if seed != "empty":
            self.seed_coverage()
        if seed == "browser":
            self.meta["material_remaining_unknown"] = self.seed_stopped_batch("unknown")
            self.meta["material_remaining_failed"] = self.seed_stopped_batch("material-failed")
            self.oa.add_affair("synthetic-material-selection", profile="efficiency_data")
            self.add_material_preview_assets("synthetic-material-selection")
            self.meta["material_query"] = self.add_query()
        self.meta["card_material_initialized"] = True
        write_metadata(self.meta_path, self.meta)
        write_metadata(self.home / "baseline-ledger.json", self.audit())

    def seed_coverage(self):
        if self.meta["material_cases"]:
            raise ValueError("Coverage seed must be fresh")
        for profile in FULL_REVIEW_PROFILES:
            case = self.add_pending_case(profile, case_key="material-" + profile, material_previews=profile == "efficiency_data")
            case.update(capability=PROFILE_BY_REVIEW[profile].prepare, evidence_boundary=OA_EVIDENCE)
            self.meta["material_cases"].append(case)
        self.meta["material_cases"].append(self.add_meeting_case(key="material-meeting"))
        for capability in MATRIX_CAPABILITIES:
            self.meta["material_cases"].append(self.add_matrix_case(capability))
        write_metadata(self.meta_path, self.meta)

    def add_material_preview_assets(self, affair_id):
        original = self.oa.data["affairs"][affair_id].get("preview_html", "")
        super().add_material_preview_assets(affair_id)
        fixture = self.oa.data["affairs"][affair_id]
        fixture["preview_html"] = original + fixture["preview_html"]
        for file_id, name, body in ((3104, "long-24-pages.pdf", text_pdf()),
                                   (3105, "scan-no-text.pdf", scanned_pdf()),
                                   (3106, "one-long-page.pdf", text_pdf(pages=1, long_page=True))):
            route = "/seeyon/fileDownload.do?" + urlencode({"method": "download", "viewMode": "download",
                "fileId": file_id, "createDate": "2026-10-23", "filename": name, "v": "1"})
            parsed = urlparse(route)
            normalized = parsed.path + "?" + urlencode(sorted(parse_qsl(parsed.query)))
            fixture["preview_files"][normalized] = {"content_type": "application/pdf", "body_base64": base64.b64encode(body).decode()}
            fixture["preview_html"] += f'<p><a href="{escape(route, quote=True)}">{name}</a></p>'
        self.oa.persist()

    def _prepare(self, capability, arguments, *, key, profile, title):
        task_id = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key="card-material|" + key, endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"], title=title)["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key="card-material:" + key, role="assistant", text="隔离材料验收：" + title, task_id=task_id)
        result = self.service.invoke(user_subject=OWNER, capability_name=capability, arguments=arguments,
            task_id=task_id, idempotency_key="material-source:" + key)
        if result["status"] != "requires_user_action" or result["interaction"]["type"] != "business_input":
            raise AssertionError(result)
        case = {"key": key, "profile": profile, "capability": capability, "arguments": arguments, "task_id": task_id,
            "input_id": result["nextAction"]["inputSubmissionId"], "input_interaction": result["interaction"]["interactionId"],
            "evidence_boundary": OA_EVIDENCE}
        self.meta["full_cases"].append(case)
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    @staticmethod
    def meeting_affair(key):
        return "-" + str(810000000000 + int(sha256(key.encode()).hexdigest()[:8], 16))

    def add_meeting_case(self, *, key=None):
        key = key or "meeting-" + uuid4().hex
        affair = self.meeting_affair(key)
        self.oa.add_affair(affair, profile="meeting_reply")
        return self._prepare("oa.meeting.reply.prepare", {"affair_id": affair}, key=key,
            profile="meeting_reply", title="会议回执独立验收 · " + key)

    def add_material_batch(self, profiles=ALL_OA_PROFILES, *, key=None):
        key = key or "batch-" + uuid4().hex
        affairs = []
        for index, profile in enumerate(profiles):
            affair = self.meeting_affair(key + ":" + str(index)) if profile == "meeting_reply" else f"synthetic-material-{key}-{index}"
            self.oa.add_affair(affair, profile=profile)
            affairs.append(affair)
        case = self._prepare("oa.workflow.pending.batch.prepare", {"affair_ids": affairs, "max_items": len(affairs)},
            key=key, profile="mixed_batch", title=f"材料混合批次 · {len(profiles)}项 · {key}")
        case.update(affair_ids=affairs, profiles=list(profiles))
        self.meta["material_batches"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def approve_case(self, case, *, values=None):
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(case["input_id"])
        credentials = {"source_submission_id": case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        state = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
            values=values or MatrixHttpWorkload.values(opened["state"]))
        preview = drafts.preview(**credentials, expected_revision=state["revision"])
        return drafts.approve(**credentials, expected_revision=preview["revision"],
            preview_id=preview["preview"]["previewId"], request_key="material-decision-" + uuid4().hex)

    def drive_case(self, case):
        interaction_id = case["input_interaction"]
        for _ in range(5):
            result = self.resume(interaction_id)
            interaction = result.get("interaction")
            if not interaction or not interaction["resume"]["ready"]:
                return result
            interaction_id = interaction["interactionId"]
        raise AssertionError("Original Central coordinator did not settle")

    def seed_stopped_batch(self, kind):
        if kind not in {"unknown", "material-failed"}:
            raise ValueError("Unsupported stop scenario")
        case = self.add_material_batch(("weekly_report", "efficiency_data", "business_settlement", "meeting_reply"), key="remaining-" + kind)
        self.approve_case(case)
        result = self.drive_case(case)
        current = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
            "input_interaction": result["interaction"]["interactionId"]}
        decision = self.approve_case(current)
        target = self.oa.data["affairs"][case["affair_ids"][1]]
        if kind == "unknown":
            target["submission_fault"] = "lost_receipt"
        else:
            target["body_text"] += "原始材料在明确决定之后变化，必须阻止实际写入。"
        self.oa.persist()
        self.drive_case(current)
        auth = self.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        if kind == "unknown":
            assert auth["state"] == "consumed"
            assert self.service.operations.get(auth["commit_operation_id"])["status"] == "unknown"
        else:
            assert auth["state"] == "approved"
        batch = self.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
        assert batch["current_ordinal"] == 2 and batch["succeeded_count"] == 1
        return {**case, "current": current, "stopKind": kind, "batchId": batch["batch_id"],
            "untouchedAffairs": case["affair_ids"][2:], "authorizationId": auth["authorization_id"]}

    def add_query(self):
        queued = queue_query(self.service, self.account, {"requestKey": "material-query:" + uuid4().hex})
        result = execute_query(self.service, OWNER, queued["taskId"], queued["commandKey"], runtime_context={
            "endpointId": self.account["endpoint_id"], "hostType": "openclaw", "hostInstanceId": "synthetic-material-host"})
        assert result["status"] == "succeeded", result
        return {"task_id": queued["taskId"], "operation_id": result["operationId"]}

    def present_case(self, case):
        return self.service.present_interaction(user_subject=OWNER, agent_host="openclaw", endpoint_key=self.account["endpoint_key"],
            interaction_id=case["input_interaction"], continuous_flow=True)["interaction"]["presentation"]

    servers = CardContentAcceptanceFixture.servers

    def audit(self):
        with self.service.tasks._connect() as connection:
            count = lambda table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            authorizations = dict(connection.execute("SELECT state,COUNT(*) FROM write_authorizations GROUP BY state"))
            decisions = count("business_draft_decisions")
            dispatches = count("agent_host_dispatches")
            batches = [dict(row) for row in connection.execute(
                "SELECT batch_id,parent_task_id,state,current_ordinal,succeeded_count,failed_count FROM task_batches ORDER BY created_at")]
        effects = {"oa": len(self.oa.writes), "taihua": self.transport.write_calls,
            "smartlight": len(self.smartlight.data["writes"]), "syntheticOaBoundary": len(self.matrix["effects"])}
        return {"decisions": decisions, "authorizations": authorizations, "effects": effects, "syntheticWrites": sum(effects.values()),
            "modelDispatches": dispatches, "batches": batches, "coordinatorErrors": list(self.coordinator_errors)}

    def public_metadata(self):
        # Access metadata stays in a 0600 file; the server prints no card ticket.
        cases = [{**case, "presentation": self.present_case(case)} for case in self.meta["material_cases"]]
        batches = [{**case, "presentation": self.present_case(case)} for case in self.meta["material_batches"]]
        return {"fixture": CONTRACT, "home": str(self.home), "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "credentials_file": str(self.credentials_path), "audit_file": str(self.home / "ledger.json"),
            "cases": cases, "batches": batches, "remainingUnknown": self.meta.get("material_remaining_unknown"),
            "remainingFailed": self.meta.get("material_remaining_failed"), "query": self.meta.get("material_query"),
            "coverage": {"fieldProfiles": 29, "pendingOaProfiles": 17, "meetingReplyProfiles": 1, "fieldlessActions": 3,
                "syntheticOaPrepareCommitBindings": sorted(SYNTHETIC_BINDINGS)},
            "run": {"platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version()},
            "pdfContract": {"longPages": 24, "searchTerm": "MATERIAL-CHECK", "uniqueFooter": "UNIQUE-FOOTER-NN",
                "scanHasText": False, "singleLongPageLines": 85},
            "evidence_boundary": "Real Central, trusted cards, Workspace, all 17 pending OA adapters and meeting reply over closed IO; eight explicitly listed legacy OA prepare/commit bindings remain synthetic"}
