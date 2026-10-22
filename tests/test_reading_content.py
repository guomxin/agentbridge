from copy import deepcopy
from html import escape
import json
from unittest.mock import Mock

import pytest

from agentbridge.auth.business_draft_card import BusinessDraftApplication
from agentbridge.auth.field_card import _render_review
from agentbridge.auth.reading_content import (
    MAX_READING_CHARACTERS, READING_TAGS, project_draft_state, project_review,
    reading_content, render_reading,
)
from agentbridge.core.business_drafts import BusinessDraftError


def nodes(reading):
    def walk(items):
        for item in items:
            if isinstance(item, dict):
                yield item
                yield from walk(item["children"])
    return list(walk(reading["nodes"]))


@pytest.mark.parametrize("source", [
    "公式 A < B > C", "请写 <strong>示例</strong>", "<script>原文</script>",
    "第一行\n第二行\n  缩进", "&lt;script&gt;示例&lt;/script&gt;", "",
])
def test_plain_comparisons_and_literal_tags_remain_complete_inert_text(source):
    text, reading = reading_content(source)
    assert text == source
    assert reading["notices"] == []
    html = render_reading(reading)
    assert escape(source) in html
    assert "<script>" not in html
    assert "<strong>" not in html


@pytest.mark.parametrize("layers", [0, 1, 2])
def test_documents_and_encoded_documents_keep_semantic_reading_order(layers):
    source = ('<!doctype html><html><head><style>PRIVATE_STYLE</style><script>PRIVATE_HEAD</script></head>'
              '<body><h1>申请材料</h1><p>开始<b>重点</b><br>第二行</p><ol><li>第一项</li><li>第二项</li></ol>'
              '<table><thead><tr><th colspan="2">金额</th></tr></thead><tbody><tr>'
              '<td rowspan="2">甲</td><td>1,200</td></tr><tr><td>300</td></tr></tbody></table>'
              '<blockquote>原文引用</blockquote><pre>代码\n  缩进</pre><script>PRIVATE_BODY</script></body></html>')
    for _ in range(layers):
        source = escape(source)
    text, reading = reading_content(source)
    assert "开始重点\n第二行" in text
    assert text.index("第一项") < text.index("第二项") < text.index("金额") < text.index("甲") < text.index("1,200")
    assert "代码\n  缩进" in text
    assert "PRIVATE" not in text
    html = render_reading(reading)
    assert '<th colspan="2">金额</th>' in html
    assert '<td rowspan="2">甲</td>' in html
    assert "<strong>重点</strong>" in html
    assert "<ol>" in html and "<thead>" in html
    assert "<script" not in html and "<style" not in html
    assert all(node["tag"] in READING_TAGS for node in nodes(reading))


def test_actual_shape_with_plain_page_prefix_and_rich_body_is_readable():
    source = "OA 页标题\n效能数据\n" + "".join(f'<p>第{i}段<b>核对材料</b></p>' for i in range(32))
    source += ''.join(f'<img src="https://private.test/{i}?token=PRIVATE_TOKEN" alt="示意图{i}" onerror="run()">' for i in range(5))
    text, reading = reading_content(source)
    assert "OA 页标题" in text
    assert "第0段核对材料" in text and "第31段核对材料" in text
    assert "<p>" not in text and "<img" not in text
    assert len([node for node in nodes(reading) if node["tag"] == "p"]) == 32
    html = render_reading(reading)
    assert "示意图0" in html and "示意图4" in html
    assert "PRIVATE_TOKEN" not in html
    assert reading["notices"]


@pytest.mark.parametrize("tag", ["div", "section", "article", "header", "footer", "address", "center"])
@pytest.mark.parametrize("prefix", ["", "OA 页面摘要\n"])
def test_structural_block_fragments_trigger_reading_independently_of_paragraph_or_document_tags(tag, prefix):
    # No p/table/list/document tag can activate the older detector here. These
    # wrappers commonly carry the entire body supplied by an editor.
    source = prefix + f'<{tag} class="source-layout">第一行<b>重点</b><br>末行</{tag}>'
    text, reading = reading_content(source)
    assert "第一行重点\n末行" in text
    assert text.endswith("末行")
    if prefix:
        assert text.startswith(prefix.strip())
    html = render_reading(reading)
    assert "<strong>重点</strong><br>末行" in html
    assert "source-layout" not in html
    assert f"&lt;{tag}" not in html
    assert reading["notices"] == []


@pytest.mark.parametrize("source", [
    "<span>普通字面示例</span>", "<em>普通字面示例</em>", "<b>普通字面示例</b>",
    "<img src=\"https://private.test\" onerror=\"run()\">",
])
def test_inline_elements_or_images_alone_do_not_expand_html_detection(source):
    text, reading = reading_content(source)
    assert text == source
    assert escape(source) in render_reading(reading)
    assert reading["notices"] == []


def test_active_elements_attributes_hidden_content_and_remote_links_never_survive():
    source = ('<html><body onload="PRIVATE_EVENT"><p style="background:url(https://private.test)">可见'
              '<a href="javascript:PRIVATE_LINK" id="draft-submit">链接说明</a></p>'
              '<div hidden>PRIVATE_HIDDEN</div><div style="display:none!important">PRIVATE_CSS</div>'
              '<p aria-hidden="true">PRIVATE_ARIA</p><template>PRIVATE_TEMPLATE</template>'
              '<iframe src="https://private.test">PRIVATE_FRAME</iframe><object>PRIVATE_OBJECT</object>'
              '<svg><script>PRIVATE_SVG</script><text>PRIVATE_SVG_TEXT</text></svg>'
              '<math><annotation-xml>PRIVATE_MATH</annotation-xml></math>'
              '<form action="https://private.test"><p>表单内可见材料</p><input value="PRIVATE_INPUT"></form>'
              '<img src="https://private.test" alt="图片说明"><img src="https://private.test">'
              '</body></html>')
    text, reading = reading_content(source)
    html = render_reading(reading)
    assert "可见链接说明" in text and "表单内可见材料" in text
    assert "图片说明" in text and "图片内容未读取" in text
    assert "PRIVATE" not in text and "PRIVATE" not in html
    assert "https:" not in html and "javascript:" not in html
    assert not any(node["tag"] in {"a", "img", "form", "input", "script", "style", "iframe", "svg", "math"} for node in nodes(reading))
    for node in nodes(reading):
        assert set(node) <= {"tag", "children", "colspan", "rowspan"}


def test_hidden_nested_paragraph_cannot_close_an_outer_paragraph_and_escape_suppression():
    text, reading = reading_content('<p>可见前文<div hidden><p>PRIVATE_HIDDEN</p></div>可见尾部</p>')
    assert "PRIVATE_HIDDEN" not in text and "PRIVATE_HIDDEN" not in render_reading(reading)
    assert "可见前文" in text and "可见尾部" in text


@pytest.mark.parametrize("value,kept", [("0", False), ("-1", False), ("101", False), ("1.2", False),
                                       ('2" onclick="x', False), ("2", True), ("100", True)])
def test_cell_merge_attributes_are_bounded_integers(value, kept):
    source = f'<table><tr><td colspan="{escape(value)}" rowspan="{escape(value)}">材料</td></tr></table>'
    _, reading = reading_content(source)
    cell = next(node for node in nodes(reading) if node["tag"] == "td")
    assert ("colspan" in cell) is kept
    assert ("rowspan" in cell) is kept


@pytest.mark.parametrize("source", [
    '<p>' + ('<span>' * 100) + '完整尾部' + ('</span>' * 100) + '</p>',
    '<p>' + ('<br>' * 17000) + '完整尾部</p>',
    '<p>' + '长' * MAX_READING_CHARACTERS + '完整尾部</p>',
])
def test_depth_node_and_character_limits_fall_back_to_full_original_without_tail_loss(source):
    text, reading = reading_content(source)
    assert text == source
    assert reading["notices"]
    assert "完整尾部" in render_reading(reading)
    assert escape(source) in render_reading(reading)


def test_partial_source_markup_is_repaired_without_reordering_or_dropping_tail():
    source = '<p>首段</p><table><tr><td>第一格<td>第二格</tr></table><p>最后一段'
    text, reading = reading_content(source)
    assert text.index("首段") < text.index("第一格") < text.index("第二格") < text.index("最后一段")
    assert "最后一段" in render_reading(reading)


def test_optional_head_end_before_explicit_body_does_not_hide_document_material():
    source = '<html><head><title>页面标题</title><meta name="x" content="PRIVATE"><body><p>实际正文</p></body></html>'
    text, reading = reading_content(source)
    assert text == "实际正文"
    assert "<p>实际正文</p>" in render_reading(reading)
    assert "PRIVATE" not in render_reading(reading)
    assert reading["notices"] == []


@pytest.mark.parametrize("source", [
    '<html><head><title>页面标题</title><p>缺少显式body的原始尾部</p></html>',
    '<html><head><title>页面标题</title><template><body><p>模板原始尾部</p></body></template></head></html>',
    '<html><body><script>只有脚本的原始尾部</script></body></html>',
    '<html><body><p hidden>隐藏内容的原始尾部</p></body></html>',
])
def test_confirmed_html_with_no_visible_projection_preserves_full_original_and_explains_fallback(source):
    text, reading = reading_content(source)
    assert text == source
    assert reading["notices"]
    html = render_reading(reading)
    assert escape(source) in html
    assert "<script>" not in html and "<template>" not in html


@pytest.mark.parametrize("reading", [
    {"version": 1, "nodes": [{"tag": "script", "children": ["private"]}], "notices": []},
    {"version": 1, "nodes": [{"tag": "p", "children": [], "onclick": "private"}], "notices": []},
    {"version": 1, "nodes": [{"tag": [], "children": []}], "notices": []},
    {"version": 1, "nodes": [{"tag": "td", "children": [], "colspan": True}], "notices": []},
    {"version": True, "nodes": [], "notices": []},
])
def test_server_renderer_rejects_untrusted_or_malformed_ast(reading):
    assert render_reading(reading) == ""


def review():
    return {"title": "办理材料", "kind": "efficiency_data", "materialsVersion": "1",
            "fields": [{"label": "事项", "value": "<p>公开标题</p>"}],
            "groups": [{"title": "补充材料", "fields": [{"label": "说明", "value": "<p>补充说明</p>"}]}],
            "sections": [{"id": "body", "title": "正文与页面材料", "kind": "text", "text": "<p>完整正文</p>"},
                         {"id": "source", "kind": "facts", "fields": [{"label": "原始字段", "value": "<p>字段内容</p>"}]},
                         {"id": "rows", "kind": "table", "columns": [{"key": "amount", "label": "金额"}],
                          "rows": [{"id": "same-row-id", "cells": {"amount": "<p>12.50</p>"}}]}],
            "attachments": [{"name": "附件.pdf"}], "completeness": {"status": "incomplete", "reasons": ["original_reason"]},
            "truncated": True}


def test_projection_covers_all_value_locations_without_mutation_and_is_idempotent():
    source = review()
    frozen = deepcopy(source)
    projected = project_review(source)
    assert source == frozen
    assert projected["fields"][0]["value"] == "公开标题"
    assert projected["groups"][0]["fields"][0]["value"] == "补充说明"
    assert projected["sections"][0]["text"] == "完整正文"
    assert projected["sections"][1]["fields"][0]["value"] == "字段内容"
    assert projected["sections"][2]["rows"][0]["cells"]["amount"] == "12.50"
    assert projected["sections"][2]["rows"][0]["readings"]["amount"]["version"] == 1
    assert projected["completeness"] == source["completeness"] and projected["truncated"] is True
    assert project_review(projected) == projected
    projected["completeness"]["reasons"].append("not_shared")
    assert source == frozen


def test_preexisting_valid_ast_must_match_display_text_before_it_can_be_reused():
    _, unrelated = reading_content('<p>另一份材料</p>')
    text, reading = reading_content('当前真实材料', unrelated)
    assert text == '当前真实材料'
    assert '当前真实材料' in render_reading(reading)
    assert '另一份材料' not in render_reading(reading)


def test_initial_application_page_projects_only_display_review_and_preserves_backend_state():
    state = {"draftId": "d", "revision": 4, "values": {"log_date": "2026-10-21", "hours": "8", "project": "", "content": "真实输入"},
             "ui": {"profileId": "taihua.work_log.create", "profileVersion": "1", "viewKind": "work_log", "review": review()},
             "preview": None, "batch": None}
    frozen = deepcopy(state)
    backend = Mock()
    backend.open.return_value = {"state": state, "csrfToken": "csrf", "maxAge": 500}
    app = BusinessDraftApplication(draft_service=backend)
    response = app.get_card("s" * 43, presentation_id=None, csrf_cookie="", secure_cookie=False)
    assert response.status == 200
    html = response.body.decode()
    assert "<p>完整正文</p>" in html
    assert "&lt;p&gt;完整正文&lt;/p&gt;" not in html
    assert 'name="content"' in html and '真实输入' in html
    assert state == frozen
    backend.save.assert_not_called()
    backend.preview.assert_not_called()
    backend.approve.assert_not_called()


@pytest.mark.parametrize("kind", ["missed_punch", "efficiency_data", "travel_expense", "business_trip",
                                 "labor_contract_renewal", "intellectual_property_declaration", "intellectual_property_reward", "overtime", "leave",
                                 "resignation", "work_handover", "flight_application", "attendance_confirmation",
                                 "weekly_report", "standard_collaboration", "quarterly_performance"])
@pytest.mark.parametrize("batch", [None, {"ordinal": 1, "totalCount": 3}])
def test_projection_has_no_profile_or_batch_gate_and_preserves_business_identity(kind, batch):
    source = {"draftId": "draft", "revision": 7, "version": 9, "batch": batch,
              "values": {"opinion": "<p>真实输入应保持不变</p>"}, "ui": {"review": {**review(), "kind": kind}},
              "preview": {"previewId": "p", "semanticReviewHash": "same-hash", "planHash": "plan-hash",
                          "revision": 7, "summary": {**review(), "kind": kind}}, "receipt": None}
    frozen = deepcopy(source)
    projected = project_draft_state(source)
    assert source == frozen
    assert projected["ui"]["review"]["sections"][0]["text"] == "完整正文"
    assert projected["preview"]["summary"]["sections"][0]["text"] == "完整正文"
    for key in ("draftId", "revision", "version", "batch", "values", "receipt"):
        assert projected[key] == frozen[key]
    for key in ("previewId", "semanticReviewHash", "planHash", "revision"):
        assert projected["preview"][key] == frozen["preview"][key]


def test_server_review_renders_source_fields_sections_and_attachments_using_closed_ast():
    source = review()
    source["sections"][0]["text"] = '<p>原始正文<b>重点</b></p><table><tr><td>真实单元格</td></tr></table><script>PRIVATE</script>'
    html = _render_review(source)
    assert "<strong>重点</strong>" in html and "<td>真实单元格</td>" in html
    assert "正文与页面材料" in html and "字段内容" in html and "12.50" in html
    assert "&lt;p&gt;原始正文" not in html
    assert "PRIVATE" not in html
    assert "附件.pdf" in html and "附件内容未在本页读取" in html


def test_application_projects_success_and_conflict_responses_only_after_backend_calls():
    state = {"revision": 3, "values": {"opinion": "原输入"}, "ui": {"review": review()},
             "preview": {"previewId": "same-preview", "summary": review()}}
    frozen = deepcopy(state)
    backend = Mock()
    backend.state.return_value = state
    backend.preview.return_value = state
    app = BusinessDraftApplication(draft_service=backend)
    response = app.state("s" * 43, presentation_id=None, token="token", csrf_cookie="token")
    assert json.loads(response.body)["ui"]["review"]["sections"][0]["text"] == "完整正文"
    response = app.mutate("s" * 43, presentation_id=None, action="preview", token="token", csrf_cookie="token",
                          content_type="application/json", body=json.dumps({"expectedRevision": 3}).encode())
    assert json.loads(response.body)["preview"]["summary"]["sections"][0]["text"] == "完整正文"
    error = BusinessDraftError("DRAFT_REVISION_CONFLICT", "版本已更新", status=409, current=state)
    response = app._error(error)
    assert json.loads(response.body)["current"]["ui"]["review"]["sections"][0]["text"] == "完整正文"
    assert state == frozen and error.current == frozen
    backend.approve.assert_not_called()
    backend.save.assert_not_called()
