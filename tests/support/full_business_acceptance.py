"""Full OA Workspace fixture with real adapters and network-free downstream IO.

Only HTTP responses and rendered downstream DOM are synthetic. Central drafts,
authorization ledgers, batch coordination, submission and verification stay real.
"""
from contextlib import contextmanager
from copy import deepcopy
from html import escape
from uuid import uuid4

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
        return rendered

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield FullOaWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class FullBusinessAcceptanceFixture(BusinessViewAcceptanceFixture):
    def __init__(self, home, *, seed=True):
        super().__init__(home, seed=False)
        self.oa = FullOaTransport(self.home / "synthetic-oa-views.json")
        self.service._worker_factories_by_system["oa"] = lambda *_: FullOaWorker(self.oa)
        grant_permissions(self.service.user_grants, OWNER, PERMISSIONS)
        self.meta.setdefault("full_cases", [])
        if seed and not self.meta["full_cases"]:
            for profile in ALL_PENDING_PROFILES:
                self.add_pending_case(profile)
        write_metadata(self.meta_path, self.meta)

    def add_pending_case(self, profile, *, case_key=None):
        case_key = case_key or uuid4().hex[:8]
        capability = {
            "missed_punch": "oa.missed_punch.approval.prepare",
            "attendance_confirmation": "oa.attendance_confirmation.prepare",
            "weekly_report": "oa.weekly_report.acknowledgement.prepare",
        }.get(profile, f"oa.{profile}.approval.prepare")
        assert capability in _TRUSTED_WRITE_DEFINITIONS
        affair_id = f"synthetic-{profile}-{case_key}"
        self.oa.add_affair(affair_id, profile=profile)
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
