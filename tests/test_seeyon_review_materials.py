"""Trusted review uses complete source evidence, while public reads stay bounded."""
from copy import deepcopy
from html import escape
from types import SimpleNamespace

import pytest

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from agentbridge.adapters.seeyon_home import parse_oa_detail
from agentbridge.adapters.seeyon_review_materials import MAX_MATERIAL_TEXT, review_materials
from tests.test_seeyon_pending_actions import ALL_PENDING_PROFILES, _inputs, _pending_case

URL = "http://oa.example.test/seeyon/collaboration/collaboration.do?method=summary&affairId=affair-1"


def parsed(body="正文", fields=()):
    rows = "".join(f"<tr><th>{escape(item['name'])}</th><td>{escape(item['value'])}</td></tr>" for item in fields)
    return parse_oa_detail(f"<html><body><h1>受控材料</h1><div>{body}</div><table>{rows}</table></body></html>", base_url=URL)


def test_long_body_fields_and_opinions_are_complete_only_on_trusted_review():
    long_body = "起始" + "长正文" * 9000 + "正文尾部"
    long_value = "本周内容" * 800 + "字段尾部"
    long_opinion = "已核对" * 800 + "意见尾部 2026-10-15 09:30"
    detail = parsed(escape(long_body) + f'<div class="processLog">合成处理人 同意 {long_opinion}</div>'
                    '<script>DO_NOT_EXPOSE_SESSION</script><a href="/attachment/download?fileId=f1">材料.pdf</a>',
                    [{"name": "本周说明", "value": long_value}, {"name": "下周计划", "value": "下周完整计划"}])
    assert len(detail["text"]) <= 20000
    assert len(detail["fields"][0]["value"]) <= 1000
    material = review_materials("weekly_report", detail)
    sections = {section["id"]: section for section in material["sections"]}
    assert "材料.pdf" in sections["body"]["text"]
    assert long_body in sections["body"]["text"]
    assert "DO_NOT_EXPOSE_SESSION" not in sections["body"]["text"]
    assert sections["current-week"]["fields"][0]["value"] == long_value
    assert sections["next-week"]["fields"][0]["value"] == "下周完整计划"
    assert long_opinion in sections["existing-opinions"]["rows"][0]["cells"]["text"]
    assert material["attachments"] == [{"name": "材料.pdf"}]
    assert material["completeness"] == {"status": "complete", "scope": "rendered_detail", "reasons": []}
    assert material["truncated"] is False


def test_full_material_keeps_inline_order_and_paragraphs():
    detail = parsed("<p>开始<strong>中间</strong>结束</p><p>下一段</p>")
    text = detail["_trusted_materials"]["text"]
    assert "开始中间结束\n下一段" in text


def test_many_line_breaks_and_void_controls_do_not_nest_or_lose_material():
    detail = parsed('<input type="hidden" value="NOT_REVIEW_DATA">' + '<br>'.join(f"第{number}行" for number in range(1200)))
    text = detail["_trusted_materials"]["text"]
    assert "第0行\n第1行" in text
    assert "第1199行" in text
    assert "NOT_REVIEW_DATA" not in text


def test_material_rows_and_attachment_names_survive_public_summary_limits():
    fields = [{"name": f"字段{number}", "value": f"完整值{number}"} for number in range(220)]
    opinions = "".join(f'<div class="processLog">处理人{number} 同意 2026-10-15 09:30</div>' for number in range(120))
    name = "长附件名" * 90 + ".pdf"
    detail = parsed(opinions + f'<a href="/attachment/download?fileId=f1">{name}</a>', fields)
    assert len(detail["fields"]) == 200
    assert len(detail["workflow"]) <= 100
    material = detail["_trusted_materials"]
    assert len(material["fields"]) == 220
    assert len(material["opinions"]) == 120
    assert material["attachments"] == [{"name": name}]
    assert material["completeness"]["status"] == "complete"


@pytest.mark.parametrize("count,name,reason", [
    (513, "合成附件", "attachment_count_limit"),
    (5, "名" * 65536, "attachment_material_limit"),
])
def test_attachment_title_attributes_obey_total_and_count_budgets(count, name, reason):
    detail = parsed("".join(f'<a title="{name}" href="/attachment/download?fileId={index}"></a>' for index in range(count)))
    material = detail["_trusted_materials"]
    assert len(material["attachments"]) <= 512
    assert sum(len(item["name"]) for item in material["attachments"]) <= MAX_MATERIAL_TEXT
    assert material["completeness"]["status"] == "incomplete"
    assert reason in material["completeness"]["reasons"]


def test_unnamed_attachment_does_not_use_private_download_query_as_display_name():
    detail = parsed('<a href="/attachment/download?fileId=synthetic-id&amp;token=synthetic-private-token"></a>')
    review = review_materials("leave", detail)
    assert review["attachments"] == [{"name": "未命名附件"}]
    assert "synthetic-private-token" not in str(review)


@pytest.mark.parametrize("profile", ["missed_punch", "weekly_report"])
def test_review_opinion_count_matches_complete_material_not_public_excerpt(profile, monkeypatch):
    adapter, worker, prepare, _, _, _ = _pending_case(profile, monkeypatch)
    resolve = adapter.resolve_workflow_detail

    def current(*args, **kwargs):
        source, detail = resolve(*args, **kwargs)
        detail = deepcopy(detail)
        rendered = parsed("".join(f'<div class="processLog">处理人{i} 同意 2026-10-15 09:30</div>' for i in range(120)), detail.get("fields", []))
        detail.update(workflow=rendered["workflow"], _trusted_materials=rendered["_trusted_materials"])
        return source, detail

    monkeypatch.setattr(adapter, "resolve_workflow_detail", current)
    summary = prepare(adapter, worker, _inputs())["summary"]
    assert next(field["value"] for field in summary["fields"] if field["label"] == "已有意见数量") == "120"
    assert len(next(section for section in summary["sections"] if section["id"] == "existing-opinions")["rows"]) == 120


def test_public_detail_does_not_expose_trusted_sections_or_unbounded_fields(monkeypatch):
    adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")
    detail = parsed("正文" * 15000, [{"name": "事由", "value": "长字段" * 5000}])
    monkeypatch.setattr(adapter, "_render_workflow_detail", lambda *args, **kwargs: ({"affair_id": "affair-1", "title": "受控材料"}, detail))
    result = adapter.get_workflow_detail(None, arguments={"collection": "pending", "affair_id": "affair-1", "text_limit": 100})
    assert len(result["detail"]["text"]) == 100
    assert result["detail"]["textTruncated"] is True
    assert len(result["detail"]["fields"][0]["value"]) <= 1000
    assert "_trusted_materials" not in str(result)
    assert "sections" not in result["detail"]


def test_missed_punch_preflight_without_opinion_binds_same_material_as_prepare(monkeypatch):
    from agentbridge.adapters.seeyon_missed_punch import preflight_missed_punch_approval

    adapter, worker, prepare, _, _, _ = _pending_case("missed_punch", monkeypatch)
    preflight = preflight_missed_punch_approval(adapter, worker, {"affair_id": "affair-1"}, "missed_punch")
    assert preflight["matched"] is True
    assert preflight["review"]["kind"] == "missed_punch"
    assert preflight["review"]["sections"]
    assert all(field["label"] != "审批意见" for field in preflight["review"]["fields"])
    assert worker.page.submitted_opinion is None
    prepared = prepare(adapter, worker, _inputs())
    assert preflight["review_fingerprint"] == prepared["plan"]["target"]["review_fingerprint"]
    assert worker.page.submitted_opinion is None


@pytest.mark.parametrize("entry", ["single", "legacy_batch", "mixed_batch"])
def test_missed_punch_initial_draft_has_full_material_before_opinion(tmp_path, entry):
    import sqlite3
    from agentbridge.core.business_drafts import BusinessDraftService
    from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture

    fixture = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        case = (fixture.add_pending_case("missed_punch") if entry == "single" else
                fixture.add_pending_batch(("missed_punch",), legacy=entry == "legacy_batch"))
        opened = BusinessDraftService(fixture.service).open(case["input_id"])
        state = opened["state"]
        assert state["values"] == {"opinion": ""}
        assert state["preview"] is None
        review = state["ui"]["review"]
        assert review["kind"] == "missed_punch"
        assert review["snapshot"] == "source"
        sections = {section["id"]: section for section in review["sections"]}
        assert "合成原始正文" in sections["body"]["text"]
        assert sections["original-fields"]["fields"]
        assert all(field["label"] != "审批意见" for field in review["fields"])
        assert review["completeness"]["status"] == "complete"
        assert fixture.oa.writes == []
        with sqlite3.connect(fixture.service.db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone() == (0,)
    finally:
        fixture.close()


@pytest.mark.parametrize("legacy,fault", [(False, None), (True, None), (False, "approval_reply_lost")])
def test_real_oa_batch_receipt_offers_only_actual_next_input_without_resuming(tmp_path, legacy, fault):
    import sqlite3
    from agentbridge.core.business_drafts import BusinessDraftService
    from agentbridge.core.trusted_flows import TrustedFlowReader
    from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture
    from tests.test_full_business_acceptance import drive

    fixture = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        case = fixture.add_pending_batch(("missed_punch", "missed_punch" if legacy else "weekly_report"), legacy=legacy)
        drafts, reader = BusinessDraftService(fixture.service), TrustedFlowReader(fixture.service)
        for ordinal in (1, 2):
            opened = drafts.open(case["input_id"])
            credentials = {"source_submission_id": case["input_id"], "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
            state = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": "合成逐项材料核对"})
            state = drafts.preview(**credentials, expected_revision=state["revision"])
            decision = drafts.approve(**credentials, expected_revision=state["revision"], preview_id=state["preview"]["previewId"], request_key="batch-next-item-review")
            fixture.oa.arm(fault)
            result = drive(fixture, case)
            with sqlite3.connect(fixture.service.db_path) as connection:
                before = connection.execute("SELECT COUNT(*) FROM operations").fetchone()
            status = reader.status(reader.context("authorize", decision["receipt"]["authorizationId"], None))
            if fault:
                assert status["state"] == "unknown"
                assert "continueUrl" not in status
                drive(fixture, case)
                assert len(fixture.oa.writes) == 1
                with sqlite3.connect(fixture.service.db_path) as connection:
                    assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == before
                break
            assert status["state"] == "completed"
            assert status["batch"]["completedCount"] == ordinal
            assert status["batch"]["totalCount"] == 2
            if ordinal == 1:
                next_id = result["nextAction"]["inputSubmissionId"]
                assert status["continueUrl"] == f"/flow/input/{next_id}"
                assert status["batch"]["nextOrdinal"] == 2
                case = {**case, "input_id": next_id, "input_interaction": result["interaction"]["interactionId"]}
            else:
                assert "continueUrl" not in status
            assert len(fixture.oa.writes) == ordinal
            with sqlite3.connect(fixture.service.db_path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == before
    finally:
        fixture.close()


@pytest.mark.parametrize("profile", ALL_PENDING_PROFILES)
@pytest.mark.parametrize("changed", ["body", "opinion", "attachment"])
def test_all_profiles_recheck_complete_material_before_commit(profile, changed, monkeypatch):
    adapter, worker, prepare, commit, contract_error, _ = _pending_case(profile, monkeypatch)
    resolve = adapter.resolve_workflow_detail
    material = {"body": "最初正文", "opinion": "最初意见", "attachment": "材料.pdf"}

    def current(*args, **kwargs):
        source, detail = resolve(*args, **kwargs)
        detail = deepcopy(detail)
        body = (escape(material["body"]) + '<div class="processLog">处理人 同意 '
                + escape(material["opinion"]) + ' 2026-10-15 09:30</div>'
                + '<a href="/attachment/download?fileId=f1">' + escape(material["attachment"]) + '</a>')
        detail["_trusted_materials"] = parsed(body, detail.get("fields", []))["_trusted_materials"]
        return source, detail

    monkeypatch.setattr(adapter, "resolve_workflow_detail", current)
    prepared = prepare(adapter, worker, _inputs())
    assert prepared["summary"]["materialsVersion"] == "1"
    assert prepared["summary"]["kind"] == profile
    material[changed] += "材料已变更"
    boundary = []
    with pytest.raises(contract_error, match="detail_fingerprint"):
        commit(adapter, worker, prepared["plan"], enter_commit_boundary=lambda: boundary.append(True))
    assert boundary == []


@pytest.mark.parametrize("profile", ALL_PENDING_PROFILES)
def test_all_profiles_reject_truncated_material_before_authorization(profile, monkeypatch):
    adapter, worker, prepare, _, contract_error, _ = _pending_case(profile, monkeypatch)
    resolve = adapter.resolve_workflow_detail
    material = parsed("超" * (MAX_MATERIAL_TEXT + 1))["_trusted_materials"]
    assert material["truncated"]

    def current(*args, **kwargs):
        source, detail = resolve(*args, **kwargs)
        detail = deepcopy(detail)
        detail["_trusted_materials"] = material
        return source, detail

    monkeypatch.setattr(adapter, "resolve_workflow_detail", current)
    with pytest.raises(contract_error, match="materials are incomplete"):
        prepare(adapter, worker, _inputs())


def test_missing_rendered_frame_marks_material_incomplete():
    adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")
    main, child = object(), object()
    page = SimpleNamespace(main_frame=main, frames=[main, child])
    snapshot = {"url": URL, "html": "<h1>外壳</h1>", "frames": []}
    worker = SimpleNamespace(page=page, rendered_snapshot=lambda *args, **kwargs: deepcopy(snapshot))
    result = adapter._render_workflow_source_detail(worker, {"href": URL})
    assert result["_trusted_materials"]["completeness"]["status"] == "incomplete"
    assert "detail_frame_unavailable" in result["_trusted_materials"]["completeness"]["reasons"]
    snapshot["frames"] = [{"url": URL, "html": "<p>真实正文</p>"}]
    result = adapter._render_workflow_source_detail(worker, {"href": URL})
    assert result["_trusted_materials"]["completeness"]["status"] == "complete"
    assert "真实正文" in result["_trusted_materials"]["text"]


@pytest.mark.parametrize("profile,ids", [("work_handover", ["handover-work", "handover-finance"]), ("flight_application", ["flight-legs"])])
def test_detail_rows_keep_stable_source_identity(profile, ids, monkeypatch):
    adapter, worker, prepare, _, _, _ = _pending_case(profile, monkeypatch)
    summary = prepare(adapter, worker, _inputs())["summary"]
    sections = {section["id"]: section for section in summary["sections"]}
    for section_id in ids:
        section = sections[section_id]
        assert section["kind"] == "table"
        assert section["columns"]
        assert section["rows"]
        assert all(row["id"] and row["cells"] for row in section["rows"])


def test_meeting_candidate_query_is_read_only_and_preserves_empty_new_profile():
    from agentbridge.adapters.seeyon_meeting import meeting_create_options, build_meeting_field_card_schema, MeetingContractMismatch
    from tests.test_seeyon_meeting import FakeAdapter, FakeMeetingWorker, _inputs, _room
    adapter = FakeAdapter()
    worker = FakeMeetingWorker(conflict=True, rooms=[_room(), _room(room_id="room-2", name="另一会议室")])
    values = _inputs(room="")
    assert meeting_create_options(adapter, worker, start_time=values["start_time"], end_time=values["end_time"]) == [{"id": "room-2", "name": "另一会议室"}]
    assert worker.mutation_count == 0
    occupied = FakeMeetingWorker(conflict=True, rooms=[_room()])
    with pytest.raises(MeetingContractMismatch, match="No OA meeting rooms"):
        build_meeting_field_card_schema(adapter, occupied, values)
    schema = build_meeting_field_card_schema(adapter, occupied, values, allow_empty=True)
    fields = {field["name"]: field for field in schema["fields"]}
    assert fields["room"]["options"] == []
    assert fields["start_time"]["value"] == values["start_time"]
    assert occupied.mutation_count == 0
