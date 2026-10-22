"""Full OA Workspace fixture with real adapters and network-free downstream IO.

Only HTTP responses and rendered downstream DOM are synthetic. Central drafts,
authorization ledgers, batch coordination, submission and verification stay real.
"""
from contextlib import contextmanager
from copy import deepcopy
import base64
from html import escape
from io import BytesIO
import struct
import threading
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED
import zlib

from agentbridge.core.user_grants import PERMISSIONS
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
from tests.authorization_fixtures import grant_permissions
from tests.support.business_view_acceptance import (
    BusinessViewAcceptanceFixture, SyntheticOaTransport, SyntheticOaWorker,
    SyntheticOaPage, OWNER, write_metadata,
)
from tests.test_seeyon_pending_actions import ALL_PENDING_PROFILES, _fixture
from tests.test_seeyon_work_handover import HandoverFrame, snapshot as handover_snapshot
from tests.test_seeyon_flight_application import FlightFrame, snapshot as flight_snapshot
from tests.test_seeyon_missed_punch import FakeAdapter as MissedAdapter, FakeApprovalPage


def _preview_png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    width, height = 320, 180
    pixels = b"".join(b"\0" + b"".join(bytes((40 + x // 2, 100 + y // 2, 170))
                                      for x in range(width)) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def _preview_pdf():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>"]
    for page, content in ((3, 4), (5, 6)):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 480 320] /Resources << /Font << /F1 7 0 R >> >> /Contents {content} 0 R >>".encode())
        stream = f"BT /F1 20 Tf 40 250 Td (SYNTHETIC OA MATERIAL) Tj 0 -40 Td (Page {(page - 1) // 2} of 2 - isolated preview acceptance) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


def oa_case(profile, affair_id):
    if profile == "missed_punch":
        source, detail = MissedAdapter().resolve_workflow_detail(None, collection="pending", affair_id="affair-1")
        fixture = {"profile": profile, "source": source, "detail": detail,
                   "signals": FakeApprovalPage().evaluate("", "affair-1")}
    elif profile in {"work_handover", "flight_application"}:
        fixture = _fixture("standard_collaboration")
        fixture["profile"] = profile
        if profile == "work_handover":
            fixture["source"]["title"] = "(自动发起)【HR】工作交接单-合成移交人-离职"
            fixture["detail"]["fields"] = [{"name": name, "value": name} for name in ("移交人", "交接类别", "工作事项", "交接内容")]
            fixture["signals"]["identity"].update(template_id="-1377138047593329703", form_app_id="5247761788960957082")
            fixture["row_snapshot"] = handover_snapshot()
        else:
            fixture["source"]["title"] = "【综合】乘坐飞机申请单"
            fixture["detail"]["fields"] = [{"name": "申请人", "value": "合成申请人"}, {"name": "出发时间", "value": "2026-10-20"}]
            fixture["signals"]["identity"].update(template_id="-2302881233144493310", form_app_id="2631521264809559746")
            fixture["row_snapshot"] = flight_snapshot()
    else:
        fixture = deepcopy(_fixture(profile))
    if profile == "leave":
        fixture["detail"]["fields"].append({"name": "证明材料", "value": "合成证明材料说明"})
    fixture["source"].update(affair_id=affair_id, sender="合成申请人", date="2026-10-15")
    fixture["detail"]["title"] = fixture["source"]["title"]
    fixture["detail"].setdefault("attachments", [])
    fixture["signals"]["identity"].update(summary_id=f"summary-{affair_id}", process_id=f"process-{affair_id}")
    if fixture["signals"]["identity"].get("form_app_id"):
        fixture["signals"]["identity"]["form_record_id"] = f"record-{affair_id}"
    fixture["processed"] = False
    fixture["body_text"] = "合成原始正文：本次材料用于隔离验收，不连接真实业务系统。\n" + "完整阅读材料。" * 180
    return fixture


class FullOaTransport(SyntheticOaTransport):
    def add_affair(self, affair_id, *, profile="leave"):
        self.data["affairs"][affair_id] = oa_case(profile, affair_id)
        self.persist()


class FullOaPage(SyntheticOaPage):
    @property
    def frames(self):
        if self.worker.affair_id:
            fixture = self.worker.fixture
            if fixture["profile"] == "work_handover":
                return [HandoverFrame(fixture["row_snapshot"])]
            if fixture["profile"] == "flight_application":
                return [FlightFrame(fixture["row_snapshot"])]
        return super().frames


class FullOaWorker(SyntheticOaWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = FullOaPage(self)

    def rendered_snapshot(self, url, **kwargs):
        rendered = super().rendered_snapshot(url, **kwargs)
        # Use real HTML parsing for complete material, not a fabricated review.
        text = escape(self.fixture.get("body_text", ""))
        rendered["html"] = rendered["html"].replace("</body>", f"<article>{text}</article></body>")
        if self.fixture.get("preview_html"):
            rendered["html"] = rendered["html"].replace("</body>", self.fixture["preview_html"] + "</body>")
        return rendered

    def request_bytes(self, method, url, **_kwargs):
        """Closed synthetic files only; this worker never opens a real socket."""
        parsed = self.transport.validate_url(url, {"/seeyon/fileUpload.do", "/seeyon/fileDownload.do",
            "/seeyon/rest/attachment/file/batchDownload/3101,3102,3103"})
        route = parsed.path + "?" + urlencode(sorted(parse_qsl(parsed.query)))
        files = self.fixture.get("preview_files", {}) if self.affair_id else {}
        if method != "GET" or route not in files:
            raise AssertionError("Synthetic material request is outside the frozen fixture")
        item = files[route]
        self.transport.data.setdefault("material_read_calls", []).append({"method": method, "route": parsed.path})
        self.transport.persist()
        return {"status": 200, "url": url, "content_type": item["content_type"],
                "body": base64.b64decode(item["body_base64"])}

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield FullOaWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class FullBusinessAcceptanceFixture(BusinessViewAcceptanceFixture):
    def __init__(self, home, *, seed=True, assign_skill=True):
        super().__init__(home, seed=False)
        self.oa = FullOaTransport(self.home / "synthetic-oa-views.json")
        self.service._worker_factories_by_system["oa"] = lambda *_: FullOaWorker(self.oa)
        grant_permissions(self.service.user_grants, OWNER, PERMISSIONS)
        settings = self.service.skills.config("user:" + OWNER)
        if assign_skill and "oa-pending" not in settings["value"]:
            self.service.skills.save("user:" + OWNER, {**settings["value"], "oa-pending": {
                "profiles": ["single", "batch"], "source_id": "", "detail": "standard"}},
                expected_revision=settings["revision"], actor="synthetic-acceptance",
                reason="Explicit isolated Workspace OA selection acceptance")
        self.meta.setdefault("full_cases", [])
        if seed and not self.meta["full_cases"]:
            for profile in ALL_PENDING_PROFILES:
                self.add_pending_case(profile)
        write_metadata(self.meta_path, self.meta)

    def _coordinate_preparations(self):
        """The isolated host drives opaque intents; production uses L3 leases."""
        from agentbridge.core.actionable_results import pending_preparations, execute_preparation
        while not self.stop_event.wait(.05):
            for command in pending_preparations(self.service, OWNER):
                try:
                    result = execute_preparation(self.service, OWNER, command["taskId"], command["commandKey"],
                        runtime_context={"endpointId": self.account["endpoint_id"], "hostType": "openclaw",
                            "hostInstanceId": "synthetic-host-coordinator"})
                    if result.get("interaction"):
                        with self.lock:
                            if not any(case["task_id"] == command["taskId"] for case in self.meta["draft_cases"]):
                                self.meta["draft_cases"].append({"task_id": command["taskId"]})
                except Exception as exc:
                    self.coordinator_errors.append(type(exc).__name__ + ": " + str(exc))
                    self.coordinator_errors[:] = self.coordinator_errors[-10:]

    def coordinate(self):
        thread = threading.Thread(target=self._coordinate_preparations, daemon=True)
        thread.start()
        try:
            super().coordinate()
        finally:
            thread.join(timeout=2)

    def add_pending_case(self, profile, *, case_key=None, material_previews=False):
        case_key = case_key or uuid4().hex[:8]
        capability = {
            "missed_punch": "oa.missed_punch.approval.prepare",
            "attendance_confirmation": "oa.attendance_confirmation.prepare",
            "weekly_report": "oa.weekly_report.acknowledgement.prepare",
        }.get(profile, f"oa.{profile}.approval.prepare")
        assert capability in _TRUSTED_WRITE_DEFINITIONS
        affair_id = f"synthetic-{profile}-{case_key}"
        self.oa.add_affair(affair_id, profile=profile)
        if material_previews:
            self.add_material_preview_assets(affair_id)
        arguments = {"affair_id": affair_id}
        title = self.oa.data["affairs"][affair_id]["source"]["title"]
        task = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key=f"full-acceptance|{profile}|{case_key}", endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"],
            title=f"{title} · {case_key}")["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key=f"full:{profile}:{case_key}", role="assistant", text="全业务独立验收 · " + title + "。下游为隔离合成材料。", task_id=task)
        result = self.service.invoke(user_subject=OWNER, capability_name=capability, arguments=arguments,
            task_id=task, idempotency_key=f"full-source:{profile}:{case_key}")
        if result["status"] != "requires_user_action" or result["interaction"]["type"] != "business_input":
            raise RuntimeError(f"Actual OA prepare did not produce a business input: {result}")
        case = {"profile": profile, "key": case_key, "task_id": task, "input_id": result["nextAction"]["inputSubmissionId"],
                "input_interaction": result["interaction"]["interactionId"], "arguments": arguments}
        self.meta["full_cases"].append(case)
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case

    def add_material_preview_assets(self, affair_id):
        """Install PNG, two-page PDF and mixed ZIP before real OA preparation."""
        image_route = "/seeyon/fileUpload.do?method=showRTE&type=image&fileId=3101&createDate=2026-10-22"
        pdf_route = "/seeyon/fileDownload.do?method=download&viewMode=download&fileId=3102&createDate=2026-10-22&filename=preview.pdf&v=1"
        zip_route = "/seeyon/rest/attachment/file/batchDownload/3101,3102,3103?zipFileName=preview.zip"
        png, pdf = _preview_png(), _preview_pdf()
        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
            zipped.writestr("材料/合成图示.png", png)
            zipped.writestr("材料/两页说明.pdf", pdf)
            zipped.writestr("费用表.xlsx", b"Synthetic spreadsheet - intentionally unsupported preview format")
        fixture = self.oa.data["affairs"][affair_id]
        fixture["preview_html"] = ('<section><h2>合成附件预览</h2><p>下列文件仅用于隔离验收。</p>'
            f'<img src="{escape(image_route, quote=True)}" alt="合成蓝绿色图示">'
            f'<a href="{escape(pdf_route, quote=True)}">合成两页说明.pdf</a>'
            f'<a href="{escape(zip_route, quote=True)}">合成附件包（PNG、PDF、表格）.zip</a></section>')
        fixture["preview_files"] = {}
        # Real OA showRTE marks PNG bytes as JPEG; preserve that observed header
        # mismatch in browser acceptance while keeping the image bytes fixed.
        for route, content_type, data in ((image_route, "image/jpeg", png), (pdf_route, "application/pdf", pdf),
                                           (zip_route, "application/zip", archive.getvalue())):
            parsed = urlparse(route)
            normalized = parsed.path + "?" + urlencode(sorted(parse_qsl(parsed.query)))
            fixture["preview_files"][normalized] = {"content_type": content_type,
                                                    "body_base64": base64.b64encode(data).decode()}
        self.oa.persist()

    def public_metadata(self):
        return {"fixture": "full-business-real-central-synthetic-downstream-v1", "workspace_url": self.workspace_url,
                "card_origin": self.card_url, "username": "cardreview", "cases": self.meta["full_cases"],
                "oa_write_count": len(self.oa.writes),
                "evidence_boundary": "Real central, adapters, HTML parsers, drafts, coordinator and readback; synthetic downstream IO only"}


    def add_pending_batch(self, profiles=ALL_PENDING_PROFILES, *, legacy=False):
        key = uuid4().hex[:8]
        affair_ids = []
        for index, profile in enumerate(profiles):
            affair_id = f"synthetic-batch-{key}-{index}"
            self.oa.add_affair(affair_id, profile=profile)
            affair_ids.append(affair_id)
        capability = "oa.missed_punch.approval.batch.prepare" if legacy else "oa.workflow.pending.batch.prepare"
        arguments = {"limit": len(profiles)} if legacy else {"affair_ids": affair_ids, "max_items": len(profiles)}
        task_id = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
            host_task_key=f"full-batch|{key}", endpoint_key=self.account["endpoint_key"], client_type="web",
            external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"],
            title=f"{'补签' if legacy else '混合'}批次单次确认验收 · {len(profiles)}项 · {key}")["task"]["taskId"]
        self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
            message_key=f"full-batch:{key}", role="assistant", text="批次独立验收。逐项确认合成材料与动作。", task_id=task_id)
        result = self.service.invoke(user_subject=OWNER, capability_name=capability, arguments=arguments,
            task_id=task_id, idempotency_key=f"full-batch-source:{key}")
        if result["status"] != "requires_user_action" or result["interaction"]["type"] != "business_input":
            raise RuntimeError(f"Actual OA batch did not produce a business input: {result}")
        case = {"profile": "legacy_batch" if legacy else "mixed_batch", "key": key, "task_id": task_id,
                "input_id": result["nextAction"]["inputSubmissionId"], "input_interaction": result["interaction"]["interactionId"],
                "arguments": arguments, "affair_ids": affair_ids}
        self.meta["full_cases"].append(case)
        self.meta["draft_cases"].append(case)
        write_metadata(self.meta_path, self.meta)
        return case
