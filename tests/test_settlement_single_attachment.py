"""Offline DOM coverage for CAP4's single-file attachment representation.

The synthetic component has a file identity distinct from its association ID.
No production names, object IDs, account data or attachment contents are used.
"""
from html import escape
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from agentbridge.adapters.seeyon_business_settlement import (
    ATTACHMENT_FIELDS,
    BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT,
    FORM_APP_ID,
    business_settlement_business_snapshot,
)
from tests.test_seeyon_business_settlement import SettlementFrame, field, snapshot


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


def _html(*, single):
    html = []
    for item in snapshot()["fields"]:
        kind, value = "", escape(item["value"])
        if item["id"] in ATTACHMENT_FIELDS:
            kind = " cap4-attach"
            if single and item["id"] == "field0017_id" and item["record_id"] == "expense-1":
                # CAP4 intentionally leaves this hidden batch-download ID empty
                # when there is exactly one attachment. aright has its filename
                # and a human-readable size suffix.
                value = (
                    '<div class="cap4-attach__browse"><div class="cap4-attach__items">'
                    '<a class="cap4-attach-downall" style="display:none" '
                    'href="/seeyon/rest/attachment/file/batchDownload/?zipFileName=synthetic-invoice.zip">'
                    '<span class="cap4-attach-downall-text">批量下载</span></a>'
                    '<div class="cap4-attach__att">'
                    '<div class="cap4-attach__action cap4-attach-action-mantle">'
                    '<div class="cap4-attach__download"><span class="cap-icon-download"></span></div>'
                    '<div class="cap4-attach__search"><span class="cap-icon-search"></span></div></div>'
                    '<div class="cap4-attach__aleft"><i class="icon iconfont pdf"></i></div>'
                    '<div class="cap4-attach__aright"><span>合成发票-101.pdf</span>'
                    '<span> (150KB)</span></div></div></div></div>'
                )
            else:
                value = " ".join(
                    f'<span class="cap4-attach__att">{escape(name)}</span>'
                    for name in item["value"].split()
                )
                if item["attachment_ids"]:
                    value += (
                        '<a class="cap4-attach-downall" '
                        'href="/seeyon/rest/attachment/file/batchDownload/'
                        + ",".join(item["attachment_ids"]) + '"></a>'
                    )
        html.append(
            f'<tr data-record-id="{item["record_id"]}"><td><div id="{item["id"]}">'
            f'<section class="is-none{kind}"><div class="field-content-wrapper">{value}</div>'
            '<textarea style="visibility:hidden">hidden duplicate</textarea></section></div></td></tr>'
        )
    return "<table>" + "".join(html) + "</table>"


@pytest.fixture
def single_page(browser):
    page = browser.new_page()
    page.set_content(_html(single=True))
    page.locator('tr[data-record-id="expense-1"] #field0017_id > section').evaluate(
        """(section, formId) => {
          const groupId = '7001';
          section.__vue__ = {
            $el: section,
            $options: {name: 'Cap4Attachment'},
            $props: {data: {
              id: 'field0017', recordId: 'expense-1', formId,
              auth: 'browse', type: 'attachment', ctrlType: 'suiAttachment',
              showValue: groupId, showValue2: groupId,
              value: [{name: '合成发票-101.pdf', size: '154420',
                type: 'application/pdf', canBrowse: '1', fileUrl: '101'}],
              attachmentInfo: {
                baseAttachmentInfo: {subReference: groupId},
                attachmentInfos: [{filename: '合成发票-101.pdf', size: '154420',
                  fileUrl: '101', id: '9001', subReference: groupId}]
              }
            }}
          };
        }""",
        FORM_APP_ID,
    )
    try:
        yield page
    finally:
        page.close()


def _read(page, script=BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT):
    extracted = page.evaluate(script)
    return business_settlement_business_snapshot(SimpleNamespace(frames=[SettlementFrame(extracted)]))


def _single_field(result):
    return field(result, "field0017_id", "expense-1")


def _mutate(page, body):
    page.locator('tr[data-record-id="expense-1"] #field0017_id > section').evaluate(
        """(section, body) => {
          const component = section.__vue__;
          const data = component.$props.data;
          const value = data.value[0];
          const info = data.attachmentInfo.attachmentInfos[0];
          const aright = section.querySelector('.cap4-attach__aright');
          const link = section.querySelector('.cap4-attach-downall');
          new Function('section', 'component', 'data', 'value', 'info', 'aright', 'link', body)(
            section, component, data, value, info, aright, link);
        }""",
        body,
    )


@pytest.mark.parametrize("href", [
    "/seeyon/rest/attachment/file/batchDownload/",
    "/seeyon/rest/attachment/file/batchDownload/?zipFileName=synthetic-invoice.zip",
])
def test_single_file_uses_matching_component_file_identity_with_empty_hidden_batch_link(single_page, href):
    _mutate(single_page, "link.setAttribute('href', " + repr(href) + ");")
    attachment = _single_field(_read(single_page))
    assert attachment["attachment_ids"] == ["101"]
    assert attachment["attachment_count"] == 1
    assert "合成发票-101.pdf" in attachment["value"]
    assert "9001" not in attachment["attachment_ids"]


def test_same_filename_replaced_file_url_changes_the_frozen_snapshot(single_page):
    original = _read(single_page)
    _mutate(single_page, "value.fileUrl = info.fileUrl = '104';")
    changed = _read(single_page)
    assert _single_field(original)["attachment_ids"] == ["101"]
    assert _single_field(changed)["attachment_ids"] == ["104"]
    assert _single_field(original)["value"] == _single_field(changed)["value"]
    assert changed != original


@pytest.mark.parametrize("change", [
    "delete section.__vue__;",
    "delete component.$options;",
    "delete component.$options.name;",
    "component.$options.name = 'UnknownAttachment';",
    "component.$el = section.parentElement;",
    "delete component.$el;",
    "delete component.$props;",
    "delete component.$props.data;",
    "delete data.id;",
    "data.id = 'field0009';",
    "delete data.recordId;",
    "data.recordId = 'expense-2';",
    "delete data.formId;",
    "data.formId = '999999';",
    "delete data.auth;",
    "data.auth = 'edit';",
    "delete data.type;",
    "data.type = 'text';",
    "delete data.ctrlType;",
    "data.ctrlType = 'suiText';",
    "delete data.value;",
    "data.value = [];",
    "data.value.push({...value});",
    "delete data.attachmentInfo;",
    "delete data.attachmentInfo.attachmentInfos;",
    "data.attachmentInfo.attachmentInfos = [];",
    "data.attachmentInfo.attachmentInfos.push({...info});",
    "delete value.fileUrl;",
    "delete info.fileUrl;",
    "value.fileUrl = '102';",
    "info.fileUrl = '102';",
    "value.fileUrl = info.fileUrl = 101;",
    "value.fileUrl = info.fileUrl = 'invalid';",
    "value.fileUrl = info.fileUrl = '1'.repeat(21);",
    "delete value.name;",
    "delete info.filename;",
    "value.name = '';",
    "info.filename = '';",
    "value.name = '另一份合成发票.pdf';",
    "info.filename = '另一份合成发票.pdf';",
    "delete value.size;",
    "delete info.size;",
    "value.size = '154421';",
    "info.size = '154421';",
    "value.size = info.size = 154420;",
    "delete data.showValue;",
    "delete data.showValue2;",
    "data.showValue = '7002';",
    "data.showValue2 = '7002';",
    "delete data.attachmentInfo.baseAttachmentInfo;",
    "delete data.attachmentInfo.baseAttachmentInfo.subReference;",
    "data.attachmentInfo.baseAttachmentInfo.subReference = '7002';",
    "delete info.subReference;",
    "info.subReference = '7002';",
    "aright.textContent = '另一份合成发票.pdf (150KB)';",
    "aright.remove();",
    "aright.insertAdjacentHTML('afterend', aright.outerHTML);",
    "link.style.display = '';",
    "link.href = '/seeyon/rest/attachment/file/batchDownload/invalid';",
    "link.href = '/seeyon/rest/attachment/file/batchDownload/?zipFileName=synthetic-invoice.zip#fragment';",
    "link.remove();",
    "link.insertAdjacentHTML('afterend', link.outerHTML);",
    "section.querySelector('.cap4-attach__att').insertAdjacentHTML('afterend', '<div class=cap4-attach__att></div>');",
])
def test_unproven_or_conflicting_single_file_identity_is_rejected(single_page, change):
    _mutate(single_page, change)
    with pytest.raises(ValueError):
        _read(single_page)


def test_existing_multi_file_batch_download_dom_retains_all_ids_and_empty_related_files(browser):
    page = browser.new_page()
    try:
        page.set_content(_html(single=False))
        result = _read(page)
        assert result == {"business_fields_browse_only": True, "fields": snapshot()["fields"]}
        assert _single_field(result)["attachment_ids"] == ["101", "102"]
        assert field(result, "field0017_id", "expense-2")["attachment_ids"] == ["-103"]
        assert field(result, "field0009_id")["attachment_ids"] == []
    finally:
        page.close()

