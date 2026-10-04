from __future__ import annotations

import hashlib
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import json
import logging
import re
import time
from copy import deepcopy
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bscli.adapters.seeyon_history_query import read_filtered_history
from bscli.adapters.seeyon_pending_batch import PENDING_BATCH_PREPARE_CAPABILITY, PENDING_BATCH_INPUT_SCHEMA
from bscli.core.query_contracts import HISTORY_QUERY_EVIDENCE_SCHEMA, oa_history_query_contract

from bscli.adapters.base import (
    AdapterAuthenticationRejected,
    AdapterLoginContractMismatch,
    AdapterLoginRequired,
    AdapterSessionCheckUnavailable,
    AdapterUnsupportedAuthMethod,
)

from bscli.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_PREPARE_CAPABILITY,
    BUSINESS_TRIP_PREPARE_INPUT_SCHEMA,
    BUSINESS_TRIP_SAVE_CAPABILITY,
    BUSINESS_TRIP_SAVE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_addressbook import (
    ADDRESSBOOK_CAPABILITIES,
    ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY,
    ADDRESSBOOK_EXPORT_CAPABILITY,
    ADDRESSBOOK_GROUP_LIST_CAPABILITY,
    ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY,
    ADDRESSBOOK_INPUT_SCHEMAS,
    ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY,
    ADDRESSBOOK_PERSON_GET_CAPABILITY,
    ADDRESSBOOK_PERSON_SEARCH_CAPABILITY,
    ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY,
    ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY,
    invoke_addressbook_capability,
)
from bscli.adapters.seeyon_business_trip_submit import (
    BUSINESS_TRIP_SUBMIT_CAPABILITY,
    BUSINESS_TRIP_SUBMIT_INPUT_SCHEMA,
    BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY,
    BUSINESS_TRIP_SUBMIT_PREPARE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_leave import (
    LEAVE_PREPARE_CAPABILITY,
    LEAVE_PREPARE_INPUT_SCHEMA,
    LEAVE_SAVE_CAPABILITY,
    LEAVE_SAVE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_leave_submit import (
    LEAVE_SUBMIT_CAPABILITY,
    LEAVE_SUBMIT_INPUT_SCHEMA,
    LEAVE_SUBMIT_PREPARE_CAPABILITY,
    LEAVE_SUBMIT_PREPARE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_meeting import (
    MEETING_CREATE_CAPABILITY,
    MEETING_CREATE_INPUT_SCHEMA,
    MEETING_CREATE_WORKFLOW,
    MEETING_PREPARE_CAPABILITY,
    MEETING_PREPARE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_meeting_room import (
    MEETING_ROOM_AVAILABILITY_CAPABILITY,
    MEETING_ROOM_CAPABILITIES,
    MEETING_ROOM_INPUT_SCHEMAS,
    MEETING_ROOM_MY_APPLICATIONS_CAPABILITY,
    invoke_meeting_room_capability,
)
from bscli.adapters.seeyon_meeting_room_application import (
    MEETING_ROOM_APPLICATION_CANCEL_CAPABILITY,
    MEETING_ROOM_APPLICATION_CANCEL_INPUT_SCHEMA,
    MEETING_ROOM_APPLICATION_CANCEL_PREPARE_CAPABILITY,
    MEETING_ROOM_APPLICATION_CANCEL_PREPARE_INPUT_SCHEMA,
    MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW,
    MEETING_ROOM_APPLICATION_CREATE_CAPABILITY,
    MEETING_ROOM_APPLICATION_CREATE_INPUT_SCHEMA,
    MEETING_ROOM_APPLICATION_CREATE_WORKFLOW,
    MEETING_ROOM_APPLICATION_PREPARE_CAPABILITY,
    MEETING_ROOM_APPLICATION_PREPARE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_missed_punch import (
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVAL_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_APPROVE_CAPABILITY,
    MISSED_PUNCH_APPROVE_INPUT_SCHEMA,
    MISSED_PUNCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_PREPARE_INPUT_SCHEMA,
    MISSED_PUNCH_SAVE_CAPABILITY,
    MISSED_PUNCH_SAVE_INPUT_SCHEMA,
)
from bscli.adapters.seeyon_pending_actions import (
    EFFICIENCY_DATA_APPROVAL_PREPARE_CAPABILITY,
    EFFICIENCY_DATA_APPROVE_CAPABILITY,
    PENDING_ACTION_CAPABILITY_DEFINITIONS,
    PENDING_ACTION_COMMIT_INPUT_SCHEMA,
    PENDING_ACTION_PREPARE_INPUT_SCHEMA,
    STANDARD_COLLABORATION_APPROVAL_PREPARE_CAPABILITY,
    STANDARD_COLLABORATION_APPROVE_CAPABILITY,
    TRAVEL_EXPENSE_APPROVAL_PREPARE_CAPABILITY,
    TRAVEL_EXPENSE_APPROVE_CAPABILITY,
    WEEKLY_REPORT_ACKNOWLEDGEMENT_PREPARE_CAPABILITY,
    WEEKLY_REPORT_ACKNOWLEDGE_CAPABILITY,
)
from bscli.adapters.seeyon_workflow_revoke import (
    WORKFLOW_REVOKE_CAPABILITY,
    WORKFLOW_REVOKE_INPUT_SCHEMA,
    WORKFLOW_REVOKE_PREPARE_CAPABILITY,
    WORKFLOW_REVOKE_PREPARE_INPUT_SCHEMA,
    _load_collection_rows,
)
from bscli.adapters.seeyon_system import SEEYON_OA_URL
from bscli.adapters.seeyon_documents import (
    DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY,
    DOCUMENT_CERTIFICATE_SEARCH_INPUT_SCHEMA,
    fetch_certificate_document as fetch_oa_certificate_document,
    fetch_certificate_documents as fetch_oa_certificate_documents,
    search_certificate_documents,
)
from bscli.adapters.seeyon_home import (
    TEMPLATE_CENTER_API_URL,
    parse_oa_detail,
    parse_pending_projection,
    parse_template_center_response,
)
from bscli.core.capability import CapabilityRegistry, CapabilitySpec


_LOGGER = logging.getLogger("uvicorn.error")


class SeeyonLoginRequired(AdapterLoginRequired):
    pass


class SeeyonAuthenticationRejected(AdapterAuthenticationRejected):
    pass


class SeeyonLoginContractMismatch(AdapterLoginContractMismatch):
    pass


class SeeyonUnsupportedAuthMethod(AdapterUnsupportedAuthMethod):
    pass


class SeeyonReadContractMismatch(RuntimeError):
    pass


class SeeyonSessionCheckUnavailable(AdapterSessionCheckUnavailable):
    pass


_AUTHENTICATION_FIELDS = [
    {
        "name": "username",
        "label": "OA 账号",
        "input_type": "text",
        "autocomplete": "username",
        "required": True,
    },
    {
        "name": "password",
        "label": "密码",
        "input_type": "password",
        "autocomplete": "current-password",
        "required": True,
    },
]

_USERNAME_SELECTORS = (
    '#login_username',
    '#loginName',
    '#username',
    '#userName',
    'input[name="login_username"]',
    'input[name="loginName"]',
    'input[name="username"]',
    'input[name="userName"]',
    'input[autocomplete="username"]',
    'input[type="text"]',
    'input:not([type])',
)

_PASSWORD_SELECTORS = (
    '#login_password1',
    '#login_password',
    '#password',
    '#pwd',
    'input[name="login_password1"]',
    'input[name="login_password"]',
    'input[name="password"]',
    'input[name="pwd"]',
    'input[type="password"]',
)

_SUBMIT_SELECTORS = (
    '#login_button',
    '#loginBtn',
    '#login',
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("登录")',
    'a:has-text("登录")',
)

_UNSUPPORTED_AUTH_SELECTORS = (
    'input[name*="captcha" i]',
    'input[id*="captcha" i]',
    'input[name*="verifyCode" i]',
    'input[id*="verifyCode" i]',
    'input[placeholder*="验证码"]',
)

_WORKFLOW_LIST_CAPABILITIES = {
    "oa.workflow.pending.list": "pending",
    "oa.workflow.sent.list": "sent",
    "oa.workflow.done.list": "done",
    "oa.workflow.tracked.list": "tracked",
}

_WORKFLOW_COLLECTIONS = frozenset(_WORKFLOW_LIST_CAPABILITIES.values())
_INTERNAL_WORKFLOW_COLLECTIONS = _WORKFLOW_COLLECTIONS

_WORKFLOW_COLLECTION_DESCRIPTIONS = {
    "pending": "List workflows waiting for the current OA user in the Pending page.",
    "sent": (
        "查询当前用户已发事项，日期范围按发起时间在 OA 服务端筛选，并自动读取筛选结果的分页。"
        "start_date/end_date 为上海时区包含边界的日期，可只传一端。keyword 在筛选结果中匹配公开字段。"
        "完整汇总使用 limit=1000 并检查 coverage.status；数量截断、分页异常或预算不足均为 partial，不能用于后续写入。"
        "不传日期时只返回已加载页，不代表全部历史。"
    ),
    "done": (
        "查询当前用户已办事项，日期范围按本人处理时间（不是流程发起时间）在 OA 服务端筛选，"
        "并自动读取筛选结果的分页。start_date/end_date 为上海时区包含边界的日期，可只传一端。"
        "keyword 在筛选结果中匹配公开字段。完整汇总使用 limit=1000 并检查 coverage.status；"
        "数量截断、分页异常或预算不足均为 partial，不能用于后续写入。不传日期时只返回已加载页。"
    ),
    "tracked": "List workflows followed by the current OA user in the Tracked page.",
}

_HISTORY_PAGE_CONTRACTS = {
    "sent": {
        "method": "listSent",
        "grid_id": "listSent",
        "manager_method": "getSentList",
        "open_from": "listSent",
        "date_fields": [
            {"abbr": "startDate", "basis": "initiated_at"},
            {"abbr": "createDate", "basis": "initiated_at"},
            {"abbr": "dealDate", "basis": "fallback_display_date"},
        ],
    },
    "done": {
        "method": "listDone",
        "grid_id": "listDone",
        "manager_method": "getDoneList",
        "open_from": "listDone",
        "date_fields": [
            {"abbr": "dealTime", "basis": "processed_at"},
            {"abbr": "dealDate", "basis": "processed_at"},
            {"abbr": "completeTime", "basis": "processed_at"},
            {"abbr": "startDate", "basis": "fallback_display_date"},
            {"abbr": "createDate", "basis": "fallback_display_date"},
        ],
    },
}

_HISTORY_GRID_EXTRACT_SCRIPT = r"""
({gridId, dateFields}) => {
  const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const host = document.getElementById(gridId);
  const gridObject = window.grid || null;
  const cellValue = (row, abbr) => {
    const cell = row.querySelector(`td[abbr="${abbr}"]`);
    if (!cell) return "";
    const titled = cell.querySelector("[title]");
    return clean((titled && titled.getAttribute("title")) || cell.textContent);
  };
  const items = [];
  for (const row of Array.from(host ? host.querySelectorAll("tr") : [])) {
    const title = cellValue(row, "subject");
    if (!title) continue;
    const action = Array.from(row.querySelectorAll("[onclick]"))
      .map((node) => node.getAttribute("onclick") || "")
      .find((value) => value.includes("showFlowChartAJax(")) || "";
    const affairMatch = action.match(/showFlowChartAJax\(\s*["']?(-?\d+)/);
    if (!affairMatch) continue;
    const trackText = cellValue(row, "isTrack");
    let date = "";
    let dateBasis = "unknown";
    for (const field of (dateFields || [])) {
      const candidate = cellValue(row, field.abbr);
      if (candidate) {
        date = candidate;
        dateBasis = field.basis;
        break;
      }
    }
    items.push({
      affair_id: affairMatch[1],
      title,
      status: cellValue(row, "currentNodesInfo") || cellValue(row, "state"),
      date,
      date_basis: dateBasis,
      category: cellValue(row, "category"),
      sender: cellValue(row, "startMemberName") || cellValue(row, "senderName"),
      is_track: /^(?:是|yes|true|1)$/i.test(trackText),
      raw_text: clean(row.textContent).slice(0, 800),
    });
  }
  const total = Number(gridObject && gridObject.p && gridObject.p.total);
  const page = Number(gridObject && gridObject.p && gridObject.p.page);
  return {
    total: Number.isFinite(total) ? total : items.length,
    page: Number.isFinite(page) ? page : 1,
    items,
  };
}
"""

_TRACKED_GRID_EXTRACT_SCRIPT = r"""
({gridId}) => {
  const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const host = document.getElementById(gridId);
  const gridObject = window.grid || null;
  const cellValue = (row, abbr) => {
    const cell = row.querySelector(`td[abbr="${abbr}"]`);
    if (!cell) return "";
    const titled = cell.querySelector("[title]");
    return clean((titled && titled.getAttribute("title")) || cell.textContent);
  };
  const items = [];
  for (const row of Array.from(host ? host.querySelectorAll("tr") : [])) {
    const title = cellValue(row, "subject");
    const checkbox = row.querySelector('input[name="workitemId"]');
    const affairId = clean((checkbox && checkbox.value) || String(row.id || "").replace(/^row/, ""));
    if (!title || !affairId || !/^-?\d+$/.test(affairId)) continue;
    const sourceAction = Array.from(row.querySelectorAll("[onclick]"))
      .map((node) => node.getAttribute("onclick") || "")
      .find((value) => value.includes("linkToTrack(")) || "";
    const sourceMatch = sourceAction.match(/linkToTrack\(\s*["']([^"']+)/);
    items.push({
      affair_id: affairId,
      title,
      status: cellValue(row, "currentNodesInfo"),
      date: cellValue(row, "createDate"),
      category: cellValue(row, "categoryLabel"),
      open_from: sourceMatch ? sourceMatch[1] : "listSent",
      raw_text: clean(row.textContent).slice(0, 800),
    });
  }
  const total = Number(gridObject && gridObject.p && gridObject.p.total);
  const page = Number(gridObject && gridObject.p && gridObject.p.page);
  return {
    total: Number.isFinite(total) ? total : items.length,
    page: Number.isFinite(page) ? page : 1,
    items,
  };
}
"""

_TRACKED_ID_EXTRACT_SCRIPT = r"""
({gridId}) => {
  const host = document.getElementById(gridId);
  const gridObject = window.grid || null;
  const affairIds = [];
  for (const checkbox of Array.from(
    host ? host.querySelectorAll('input[name="workitemId"]') : []
  )) {
    const affairId = String(checkbox.value || "").trim();
    if (/^-?\d+$/.test(affairId) && !affairIds.includes(affairId)) {
      affairIds.push(affairId);
    }
  }
  const total = Number(gridObject && gridObject.p && gridObject.p.total);
  const page = Number(gridObject && gridObject.p && gridObject.p.page);
  return {
    total: Number.isFinite(total) ? total : affairIds.length,
    page: Number.isFinite(page) ? page : 1,
    affair_ids: affairIds,
  };
}
"""
_WORKFLOW_LIST_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string"},
        "limit": {"type": "integer"},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
    },
    "additionalProperties": False,
}

_WORKFLOW_LIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "collection": {"type": "string"},
        "source": {"type": "string"},
        "source_count": {"type": "integer"},
        "date_filtered_count": {"type": "integer"},
        "unparsed_date_count": {"type": "integer"},
        "matched_count": {"type": "integer"},
        "count": {"type": "integer"},
        "total": {"type": ["integer", "null"]},
        "page": {"type": ["integer", "null"]},
        "coverage": {
            "type": "object",
            "properties": {
                **HISTORY_QUERY_EVIDENCE_SCHEMA,
                "status": {"type": "string"},
                "queryApplied": {"type": "boolean"},
                "dateBasis": {"type": "string"},
                "requestedRange": {
                    "type": "object",
                    "properties": {
                        "start": {"type": ["string", "null"]},
                        "end": {"type": ["string", "null"]},
                    },
                    "required": ["start", "end"],
                    "additionalProperties": False,
                },
                "scannedCount": {"type": "integer"},
                "matchedCount": {"type": "integer"},
                "hasMore": {"type": "boolean"},
                "completionReason": {"type": "string"},
                "observedAt": {"type": "string"},
                "queryHash": {"type": "string"},
            },
            "required": [
                "status", "queryApplied", "dateBasis", "requestedRange",
                "scannedCount", "matchedCount", "hasMore", "completionReason",
                "observedAt", "queryHash"
            ],
            "additionalProperties": False,
        },
        "filters": {
            "type": "object",
            "properties": {
                "keyword": {"type": ["string", "null"]},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "limit": {"type": "integer"},
            },
            "required": ["keyword", "start_date", "end_date", "limit"],
            "additionalProperties": False,
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "affair_id": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "string"},
                    "date": {"type": "string"},
                    "category": {"type": "string"},
                    "sender": {"type": "string"},
                    "read": {"type": "boolean"},
                },
                "required": [
                    "affair_id",
                    "title",
                    "date",
                    "category",
                ],
                "additionalProperties": False,
            },
        },
        "transport": {"type": "string"},
    },
    "required": [
        "schema_version",
        "collection",
        "source",
        "source_count",
        "date_filtered_count",
        "unparsed_date_count",
        "matched_count",
        "count",
        "total",
        "page",
        "coverage",
        "filters",
        "items",
        "transport",
    ],
    "additionalProperties": False,
}

_WORKFLOW_DETAIL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "collection": {"type": "string"},
        "affair_id": {"type": "string"},
        "text_limit": {"type": "integer"},
    },
    "required": ["collection", "affair_id"],
    "additionalProperties": False,
}


def _workflow_list_input_schema(collection: str) -> dict:
    schema = deepcopy(_WORKFLOW_LIST_INPUT_SCHEMA)
    if collection in {"done", "sent"}:
        schema["x-agentbridge-query"] = oa_history_query_contract(collection)
        label = "本人处理日期" if collection == "done" else "发起日期"
        for name, boundary in (("start_date", "起始"), ("end_date", "截止")):
            schema["properties"][name].update({
                "format": "date",
                "description": f"{label}的{boundary}日期，包含当天，YYYY-MM-DD；自动下推 OA 服务端。",
            })
        schema["properties"]["limit"].update({
            "minimum": 1, "maximum": 1000, "default": 50,
            "description": "最多返回的匹配条数；完整汇总建议 1000，并核对 coverage.status。",
        })
    return schema


_WORKFLOW_OPINIONS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "collection": {"type": "string"},
        "affair_id": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["collection", "affair_id"],
    "additionalProperties": False,
}


def build_central_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            name="oa.template.list",
            version="0.1.0",
            description="List templates available to the current OA user.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effect="read",
            adapter="seeyon-central",
            workflow="template-list-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY,
            version="0.1.0",
            description=(
                "Search patent and software-copyright certificate scans by name in "
                "OA Document Center and issue short-lived trusted download links."
            ),
            input_schema=DOCUMENT_CERTIFICATE_SEARCH_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="read",
            adapter="seeyon-central",
            workflow="certificate-document-search-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=BUSINESS_TRIP_PREPARE_CAPABILITY,
            version="0.3.0",
            description=(
                "Collect business-trip fields through a trusted card, validate the live "
                "OA form, and create a separate one-time confirmation card."
            ),
            input_schema=BUSINESS_TRIP_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="business-trip-draft-prepare-v2",
        )
    )
    for capability_name, description, workflow in (
        (
            MEETING_ROOM_AVAILABILITY_CAPABILITY,
            "List OA meeting rooms and their occupancy for an optional exact time range.",
            "meeting-room-availability-list-v1",
        ),
        (
            MEETING_ROOM_MY_APPLICATIONS_CAPABILITY,
            "List the authenticated user's meeting-room applications with OA-side filters.",
            "meeting-room-my-applications-list-v1",
        ),
    ):
        registry.register(
            CapabilitySpec(
                name=capability_name,
                version="0.1.0",
                description=description,
                input_schema=MEETING_ROOM_INPUT_SCHEMAS[capability_name],
                output_schema={"type": "object"},
                effect="read",
                adapter="seeyon-central",
                workflow=workflow,
            )
        )
    registry.register(
        CapabilitySpec(
            name=BUSINESS_TRIP_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume a trusted authorization once, save the frozen business-trip "
                "plan as an OA wait-send draft, and verify it by server readback."
            ),
            input_schema=BUSINESS_TRIP_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="business-trip-draft-save-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=BUSINESS_TRIP_SUBMIT_PREPARE_CAPABILITY,
            version="0.3.0",
            description=(
                "Collect business-trip fields through a trusted card, validate the live "
                "OA form and sent-item baseline, and create a separate submit authorization."
            ),
            input_schema=BUSINESS_TRIP_SUBMIT_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="business-trip-submit-prepare-v2",
        )
    )
    registry.register(
        CapabilitySpec(
            name=BUSINESS_TRIP_SUBMIT_CAPABILITY,
            version="0.2.0",
            description=(
                "Consume one trusted authorization, submit the frozen business-trip "
                "request, and verify one new readable item in the OA sent collection."
            ),
            input_schema=BUSINESS_TRIP_SUBMIT_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="business-trip-submit-commit-v2",
        )
    )
    registry.register(
        CapabilitySpec(
            name=LEAVE_PREPARE_CAPABILITY,
            version="0.2.0",
            description=(
                "Collect supported leave-request fields through a trusted card, validate "
                "the live OA form, and create a separate draft-save authorization."
            ),
            input_schema=LEAVE_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="leave-draft-prepare-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=LEAVE_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, save the frozen leave request as an "
                "OA wait-send draft, and verify it by server readback without submission."
            ),
            input_schema=LEAVE_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="leave-draft-save-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=LEAVE_SUBMIT_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Collect supported leave-request fields through a trusted card, validate "
                "the live OA form and sent-item baseline, and create a submit authorization."
            ),
            input_schema=LEAVE_SUBMIT_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="leave-submit-prepare-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=LEAVE_SUBMIT_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, submit the frozen leave request, "
                "and verify one new readable item in the OA sent collection."
            ),
            input_schema=LEAVE_SUBMIT_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="leave-submit-commit-v1",
        )
    )
    for spec in (
        CapabilitySpec(
            name=MISSED_PUNCH_PREPARE_CAPABILITY,
            version="0.2.0",
            description=(
                "Collect missed-punch fields in a trusted card, validate the live OA "
                "form, and create a separate draft-save authorization."
            ),
            input_schema=MISSED_PUNCH_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="missed-punch-draft-prepare-v1",
        ),
        CapabilitySpec(
            name=MISSED_PUNCH_SAVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, save the frozen missed-punch plan "
                "as an OA wait-send draft, and verify it without submitting approval."
            ),
            input_schema=MISSED_PUNCH_SAVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="reversible_write",
            adapter="seeyon-central",
            workflow="missed-punch-draft-save-v1",
        ),
        CapabilitySpec(
            name=MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
            version="0.2.0",
            description=(
                "Collect an approval opinion in a trusted card, validate one exact "
                "pending missed-punch item, and create a separate approval authorization."
            ),
            input_schema=MISSED_PUNCH_APPROVAL_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="missed-punch-approval-prepare-v1",
        ),
        CapabilitySpec(
            name=MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Compatibility entry; use oa.workflow.pending.batch.prepare for new batches. "
                "Freeze up to ten current pending missed-punch items and process "
                "them sequentially with independent trusted input and authorization."
            ),
            input_schema=MISSED_PUNCH_APPROVAL_BATCH_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="missed-punch-approval-batch-prepare-v1",
        ),
        CapabilitySpec(
            name=PENDING_BATCH_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Freeze a variable-length selection of current OA pending items, including mixed supported "
                "workflow types. Process each with independent trusted input, authorization and verification. "
                "Use this for multiple/all pending items instead of promising to continue singular prepares. "
                "Incomplete sources, unsupported selections and overflow stop before any approval."
            ),
            input_schema=PENDING_BATCH_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="pending-batch-prepare-v1",
        ),
        CapabilitySpec(
            name=MISSED_PUNCH_APPROVE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, approve the frozen missed-punch "
                "target, and verify that it left the pending collection."
            ),
            input_schema=MISSED_PUNCH_APPROVE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="missed-punch-approval-commit-v1",
        ),
        *MEETING_CREATE_WORKFLOW.capability_specs(),
        *MEETING_ROOM_APPLICATION_CREATE_WORKFLOW.capability_specs(),
        *MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW.capability_specs(),
    ):
        registry.register(spec)
    for definition in PENDING_ACTION_CAPABILITY_DEFINITIONS:
        profile_name = definition["profile"].replace("_", " ")
        workflow_prefix = definition["workflow_prefix"]
        action_kind = definition["action_kind"]
        registry.register(
            CapabilitySpec(
                name=definition["prepare_capability"],
                version="0.1.0",
                description=(
                    f"Collect a trusted opinion, validate one exact pending "
                    f"{profile_name} item, and create separate {action_kind} confirmation."
                ),
                input_schema=PENDING_ACTION_PREPARE_INPUT_SCHEMA,
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="seeyon-central",
                workflow=f"{workflow_prefix}-prepare-v1",
            )
        )
        registry.register(
            CapabilitySpec(
                name=definition["commit_capability"],
                version="0.1.0",
                description=(
                    f"Consume one trusted authorization, process the frozen "
                    f"{profile_name} item, and verify pending disappearance."
                ),
                input_schema=PENDING_ACTION_COMMIT_INPUT_SCHEMA,
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="seeyon-central",
                workflow=f"{workflow_prefix}-commit-v1",
            )
        )
    registry.register(
        CapabilitySpec(
            name=WORKFLOW_REVOKE_PREPARE_CAPABILITY,
            version="0.1.0",
            description=(
                "Collect a revoke comment in a trusted card, resolve one exact active "
                "sent workflow, run non-destructive OA eligibility checks, and create "
                "a separate revoke authorization."
            ),
            input_schema=WORKFLOW_REVOKE_PREPARE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="workflow-revoke-prepare-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name=WORKFLOW_REVOKE_CAPABILITY,
            version="0.1.0",
            description=(
                "Consume one trusted authorization, revoke the frozen sent workflow "
                "through OA's native action, and verify its revoked wait-send state."
            ),
            input_schema=WORKFLOW_REVOKE_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="controlled_write",
            adapter="seeyon-central",
            workflow="workflow-revoke-commit-v1",
        )
    )
    for capability_name, collection in _WORKFLOW_LIST_CAPABILITIES.items():
        registry.register(
            CapabilitySpec(
                name=capability_name,
                version="0.4.0" if collection in {"done", "sent"} else "0.3.0",
                description=_WORKFLOW_COLLECTION_DESCRIPTIONS[collection],
                input_schema=_workflow_list_input_schema(collection),
                output_schema=_WORKFLOW_LIST_OUTPUT_SCHEMA,
                effect="read",
                adapter="seeyon-central",
                workflow="workflow-list-v2",
            )
        )
    registry.register(
        CapabilitySpec(
            name="oa.workflow.detail.get",
            version="0.1.0",
            description="Get a rendered OA workflow detail by opaque affair ID.",
            input_schema=_WORKFLOW_DETAIL_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="read",
            adapter="seeyon-central",
            workflow="workflow-detail-v1",
        )
    )
    registry.register(
        CapabilitySpec(
            name="oa.workflow.opinions.list",
            version="0.1.0",
            description="List the rendered opinions for an OA workflow.",
            input_schema=_WORKFLOW_OPINIONS_INPUT_SCHEMA,
            output_schema={"type": "object"},
            effect="read",
            adapter="seeyon-central",
            workflow="workflow-opinions-v1",
        )
    )
    addressbook_descriptions = {
        ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY: (
            "List the visible OA organization and department hierarchy."
        ),
        ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY: (
            "List visible members of one OA department, optionally including descendants."
        ),
        ADDRESSBOOK_PERSON_SEARCH_CAPABILITY: (
            "Search the visible OA organization directory by name or multiple fields."
        ),
        ADDRESSBOOK_PERSON_GET_CAPABILITY: (
            "Resolve one exact visible OA person returned by address-book search."
        ),
        ADDRESSBOOK_GROUP_LIST_CAPABILITY: (
            "List visible private, personal, system, and project address-book groups."
        ),
        ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY: (
            "List visible members of one exact OA address-book group."
        ),
        ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY: (
            "Search the current OA user's private contacts without changing them."
        ),
        ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY: (
            "Resolve one exact private contact returned by private-contact search."
        ),
        ADDRESSBOOK_EXPORT_CAPABILITY: (
            "Export a bounded visible OA address-book result as a governed CSV artifact."
        ),
    }
    for capability_name, description in addressbook_descriptions.items():
        registry.register(
            CapabilitySpec(
                name=capability_name,
                version="0.1.0",
                description=description,
                input_schema=ADDRESSBOOK_INPUT_SCHEMAS[capability_name],
                output_schema={"type": "object"},
                effect="read",
                adapter="seeyon-central",
                workflow="addressbook-read-v1",
            )
        )
    return registry


class SeeyonCentralAdapter:
    def __init__(self, *, base_url: str = SEEYON_OA_URL) -> None:
        self.base_url = base_url
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Seeyon base URL must include an http(s) origin")
        self.origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        self.template_center_url = urljoin(base_url, TEMPLATE_CENTER_API_URL)

    def authentication_contract(self) -> dict:
        fingerprint_input = {
            "version": "seeyon-form-login-v1",
            "origin": self.origin,
            "fields": _AUTHENTICATION_FIELDS,
            "username_selectors": _USERNAME_SELECTORS,
            "password_selectors": _PASSWORD_SELECTORS,
            "submit_selectors": _SUBMIT_SELECTORS,
        }
        digest = hashlib.sha256(
            json.dumps(
                fingerprint_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "system_id": "oa",
            "system_name": "致远 OA",
            "origin": self.origin,
            "page_fingerprint": f"seeyon-form-login-v1:{digest}",
            "fields": [dict(field) for field in _AUTHENTICATION_FIELDS],
        }

    def authenticate(
        self,
        worker,
        credentials: dict,
        *,
        timeout_seconds: float = 45,
    ) -> dict:
        if set(credentials) != {"username", "password"}:
            raise ValueError("Seeyon authentication requires username and password")
        username = credentials.get("username")
        password = credentials.get("password")
        if not isinstance(username, str) or not username or len(username) > 256:
            raise ValueError("Seeyon username is invalid")
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise ValueError("Seeyon password is invalid")

        worker.clear_session_state()
        self.open_login(worker)
        page = worker.page
        contract_deadline = time.monotonic() + min(max(timeout_seconds, 1), 15)
        login_frame = username_locator = password_locator = None
        while time.monotonic() < contract_deadline:
            frames = list(getattr(page, "frames", []) or [page])
            if any(_has_visible(frame, _UNSUPPORTED_AUTH_SELECTORS) for frame in frames):
                raise SeeyonUnsupportedAuthMethod(
                    "The OA login page requires a verification challenge not supported by this card."
                )
            login_frame, username_locator, password_locator = _find_login_form(frames)
            if login_frame is not None:
                break
            time.sleep(0.1)
        if login_frame is None or username_locator is None or password_locator is None:
            raise SeeyonLoginContractMismatch(
                "The OA login page no longer matches the registered form contract."
            )
        username_locator.fill(username)
        password_locator.fill(password)
        submit_locator = _find_visible([login_frame], _SUBMIT_SELECTORS)
        if submit_locator is not None:
            submit_locator.click()
        else:
            password_locator.press("Enter")

        deadline = time.monotonic() + max(timeout_seconds, 1)
        while time.monotonic() < deadline:
            try:
                templates = self.list_templates(worker)
            except (SeeyonLoginRequired, SeeyonSessionCheckUnavailable):
                time.sleep(0.25)
                continue
            observed_principal = _principal_from_title(worker.page_title)
            if observed_principal:
                return {
                    "templates": templates,
                    "observed_principal_ref": observed_principal,
                    "page_url": worker.page_url,
                }
            time.sleep(0.25)
        raise SeeyonAuthenticationRejected(
            "The OA login was not accepted before the authentication challenge expired."
        )

    def list_templates(self, worker) -> dict:
        try:
            response = worker.request("GET", self.template_center_url)
        except Exception as exc:
            raise SeeyonSessionCheckUnavailable(
                f"OA session check request failed ({_safe_exception_code(exc)})."
            ) from exc
        status = int(response.get("status") or 0)
        final_url = str(response.get("url") or "")
        payload = response.get("json")
        text = str(response.get("text") or "")
        if status in {301, 302, 303, 307, 308, 401, 403} or _looks_like_login_url(
            final_url
        ):
            raise SeeyonLoginRequired("The central OA session is not logged in or has expired.")
        if status < 200 or status >= 300:
            diagnostics = _safe_response_diagnostics(response)
            if status in {408, 425, 429} or status >= 500:
                raise SeeyonSessionCheckUnavailable(
                    f"OA session check received a temporary response ({diagnostics})."
                )
            raise SeeyonReadContractMismatch(
                f"The OA template center returned an unexpected response ({diagnostics})."
            )
        if not isinstance(payload, dict):
            if _looks_like_login_html(text):
                raise SeeyonLoginRequired(
                    "The central OA session is not logged in or has expired."
                )
            raise SeeyonSessionCheckUnavailable(
                "OA session check did not return JSON "
                f"({_safe_response_diagnostics(response)})."
            )
        result = parse_template_center_response(payload, base_url=self.base_url)
        return {
            **result,
            "transport": "central_http_session",
        }

    def probe_session(self, worker) -> dict:
        templates = self.list_templates(worker)
        return {
            "authenticated": True,
            "template_count": int(templates.get("count") or 0),
            "transport": templates["transport"],
        }

    def invoke_capability(self, capability_name: str, worker, arguments: dict) -> dict:
        if capability_name == "oa.template.list":
            if arguments:
                raise ValueError("oa.template.list does not accept arguments")
            return self.list_templates(worker)
        if capability_name == DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY:
            return search_certificate_documents(
                worker,
                base_url=self.base_url,
                arguments=arguments,
            )
        if capability_name in MEETING_ROOM_CAPABILITIES:
            return invoke_meeting_room_capability(
                capability_name,
                self,
                worker,
                arguments,
            )
        collection = _WORKFLOW_LIST_CAPABILITIES.get(capability_name)
        if collection:
            return self.list_workflows(worker, collection=collection, arguments=arguments)
        if capability_name == "oa.workflow.detail.get":
            return self.get_workflow_detail(worker, arguments=arguments)
        if capability_name == "oa.workflow.opinions.list":
            return self.list_workflow_opinions(worker, arguments=arguments)
        if capability_name in ADDRESSBOOK_CAPABILITIES:
            return invoke_addressbook_capability(
                capability_name,
                worker,
                base_url=self.base_url,
                arguments=arguments,
            )
        raise KeyError(f"unsupported Seeyon central capability: {capability_name}")

    def fetch_certificate_document(self, worker, reference: dict) -> dict:
        return fetch_oa_certificate_document(
            worker,
            base_url=self.base_url,
            reference=reference,
        )

    def fetch_certificate_documents(
        self,
        worker,
        references: list[dict],
    ) -> list[dict | Exception]:
        return fetch_oa_certificate_documents(
            worker,
            base_url=self.base_url,
            references=references,
        )

    def list_workflows(self, worker, *, collection: str, arguments: dict | None = None) -> dict:
        collection = _validated_internal_collection(collection)
        arguments = arguments or {}
        keyword = _validated_optional_string(arguments.get("keyword"), "keyword", maximum=200)
        limit = _validated_integer(
            arguments.get("limit"), "limit", default=50, minimum=1,
            maximum=1000 if collection in {"done", "sent"} else 100,
        )
        start_date = _validated_optional_date(arguments.get("start_date"), "start_date")
        end_date = _validated_optional_date(arguments.get("end_date"), "end_date")
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        parsed = self._fetch_workflow_collection(
            worker, collection, start_date=start_date, end_date=end_date
        )
        expected_date_basis = {
            "done": "processed_at",
            "sent": "initiated_at",
        }.get(collection, "collection_display_date")
        raw_items = [item for item in parsed.get("items") or [] if isinstance(item, dict)]
        date_basis_is_exact = all(
            str(item.get("date_basis") or "unknown") == expected_date_basis
            for item in raw_items
        )
        public_items = [_public_workflow_item(item, collection) for item in parsed.get("items") or []]
        public_items = [item for item in public_items if item.get("title")]
        source_count = len(public_items)
        unparsed_date_count = 0
        if start_date or end_date:
            date_filtered_items = []
            for item in public_items:
                item_date = _workflow_item_date(item.get("date"))
                if item_date is None:
                    unparsed_date_count += 1
                    continue
                if start_date and item_date < start_date:
                    continue
                if end_date and item_date > end_date:
                    continue
                date_filtered_items.append(item)
            public_items = date_filtered_items
        date_filtered_count = len(public_items)
        if keyword:
            needle = keyword.casefold()
            public_items = [
                item
                for item in public_items
                if needle in " ".join(str(value) for value in item.values()).casefold()
            ]
        matched_count = len(public_items)
        public_items = public_items[:limit]
        output_truncated = matched_count > len(public_items)
        total = parsed.get("total")
        numeric_total = int(total) if isinstance(total, int) else None
        source_exhausted = (
            numeric_total is not None and numeric_total == source_count
            and int(parsed.get("page") or 1) == 1
        )
        evidence = parsed.get("query_evidence") or {}
        if evidence:
            source_exhausted = evidence.get("complete") is True
        query_applied = bool(start_date or end_date)
        date_contract_satisfied = bool(
            not query_applied
            or (unparsed_date_count == 0 and date_basis_is_exact)
        )
        coverage_complete = date_contract_satisfied and source_exhausted and not output_truncated
        has_more = bool(
            output_truncated
            or (
                not coverage_complete
                and not source_exhausted
                and (numeric_total is None or numeric_total > source_count)
            )
        )
        completion_reason = (
            "result_limit_truncated"
            if output_truncated
            else str(evidence["completionReason"])
            if evidence
            else "date_basis_not_proven"
            if query_applied and not date_basis_is_exact
            else "unparsed_dates_in_scanned_rows"
            if query_applied and unparsed_date_count
            else "source_exhausted"
            if source_exhausted
            else "loaded_page_does_not_cover_requested_range"
            if query_applied
            else "collection_not_exhausted"
        )
        query_identity = {
            "collection": collection,
            "keyword": keyword or None,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "limit": limit,
        }
        coverage = {
            "status": "complete" if coverage_complete else "partial",
            "queryApplied": query_applied,
            "serverFilterApplied": evidence.get("serverFilterApplied") is True,
            "sourceQueryTotal": evidence.get("sourceQueryTotal"),
            "sourceQueryPages": evidence.get("sourceQueryPages"),
            "pagesFetched": int(evidence.get("pagesFetched", 1)),
            "scanBudget": int(evidence.get("scanBudget", source_count)),
            "dateBasis": expected_date_basis,
            "requestedRange": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None,
            },
            "scannedCount": source_count,
            "matchedCount": matched_count,
            "hasMore": has_more,
            "completionReason": completion_reason,
            "observedAt": datetime.now().astimezone().isoformat(),
            "queryHash": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    query_identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        return {
            "schema_version": "bscli.oa_workflow_list.v2",
            "collection": collection,
            "source": parsed.get("source") or "section_api",
            "source_count": source_count,
            "date_filtered_count": date_filtered_count,
            "unparsed_date_count": unparsed_date_count,
            "matched_count": matched_count,
            "count": len(public_items),
            "total": total,
            "page": parsed.get("page"),
            "coverage": coverage,
            "filters": {
                "keyword": keyword or None,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "limit": limit,
            },
            "items": public_items,
            "transport": (
                "central_http_session"
                if collection == "pending"
                else "central_browser_session"
            ),
        }

    def get_workflow_detail(self, worker, *, arguments: dict) -> dict:
        collection = _validated_collection(arguments.get("collection"))
        affair_id = _validated_identifier(arguments.get("affair_id"), "affair_id")
        text_limit = _validated_integer(
            arguments.get("text_limit"),
            "text_limit",
            default=6000,
            minimum=0,
            maximum=20000,
        )
        source_item, parsed_detail = self._render_workflow_detail(
            worker,
            collection=collection,
            affair_id=affair_id,
        )
        opinions = _public_opinions(parsed_detail.get("workflow"))
        attachments = [
            {"name": _public_text(item.get("name"))}
            for item in parsed_detail.get("attachments") or []
            if isinstance(item, dict) and item.get("name")
        ]
        fields = [
            {
                "name": _public_text(item.get("name")),
                "value": _public_text(item.get("value")),
            }
            for item in parsed_detail.get("fields") or []
            if isinstance(item, dict) and item.get("name")
        ]
        return {
            "schema_version": "bscli.oa_workflow_detail.v1",
            "collection": collection,
            "source_item": _public_workflow_item(source_item, collection),
            "detail": {
                "title": _public_text(source_item.get("title") or parsed_detail.get("title")),
                "text": str(parsed_detail.get("text") or "")[:text_limit],
                "fields": fields,
                "field_count": len(fields),
                "attachments": attachments,
                "attachment_count": len(attachments),
                "opinions": opinions,
                "opinion_count": len(opinions),
            },
            "transport": "central_browser_session",
        }

    def list_workflow_opinions(self, worker, *, arguments: dict) -> dict:
        collection = _validated_collection(arguments.get("collection"))
        affair_id = _validated_identifier(arguments.get("affair_id"), "affair_id")
        limit = _validated_integer(arguments.get("limit"), "limit", default=100, minimum=1, maximum=100)
        source_item, parsed_detail = self._render_workflow_detail(
            worker,
            collection=collection,
            affair_id=affair_id,
        )
        opinions = _public_opinions(parsed_detail.get("workflow"))[:limit]
        return {
            "schema_version": "bscli.oa_workflow_opinions.v1",
            "collection": collection,
            "source_item": _public_workflow_item(source_item, collection),
            "count": len(opinions),
            "items": opinions,
            "transport": "central_browser_session",
        }

    def resolve_workflow_detail(
        self,
        worker,
        *,
        collection: str,
        affair_id: str,
    ) -> tuple[dict, dict]:
        """Resolve one workflow for a process adapter without exposing its URL."""
        return self._render_workflow_detail(
            worker,
            collection=_validated_internal_collection(collection),
            affair_id=_validated_identifier(affair_id, "affair_id"),
        )

    def load_sent_workflow_rows(self, worker) -> tuple[list[dict], object]:
        """Load the authoritative sent grid for governed write verification."""
        return _load_collection_rows(self, worker, collection="sent")

    def resolve_sent_workflow_row_detail(
        self,
        worker,
        *,
        source_item: dict,
    ) -> tuple[dict, dict]:
        affair_id = _validated_identifier(source_item.get("affair_id"), "affair_id")
        normalized_item = dict(source_item)
        normalized_item["affair_id"] = affair_id
        normalized_item["href"] = urljoin(
            self.base_url,
            "collaboration/collaboration.do?"
            + urlencode(
                {
                    "method": "summary",
                    "openFrom": "listSent",
                    "affairId": affair_id,
                    "showTab": "true",
                }
            ),
        )
        return normalized_item, self._render_workflow_source_detail(
            worker, normalized_item
        )

    def _fetch_workflow_collection(
        self, worker, collection: str, *, start_date: date | None = None, end_date: date | None = None
    ) -> dict:
        self.list_templates(worker)
        if collection in {"done", "sent"} and (start_date or end_date):
            return self._read_history_page(
                worker, collection, start_date=start_date, end_date=end_date
            )
        if collection != "pending":
            return self._fetch_history_page_collection(worker, collection)

        section_url = self._discover_pending_section_url(worker)
        response = worker.request("GET", section_url)
        status = int(response.get("status") or 0)
        final_url = str(response.get("url") or "")
        payload = response.get("json")
        if status in {301, 302, 303, 307, 308, 401, 403} or _looks_like_login_url(final_url):
            raise SeeyonLoginRequired("The central OA session expired while reading workflows.")
        if status < 200 or status >= 300:
            raise SeeyonReadContractMismatch(f"The OA workflow section returned HTTP {status}.")
        if not isinstance(payload, dict):
            raise SeeyonReadContractMismatch("The OA workflow section did not return JSON.")
        if not isinstance(payload.get("Data"), dict):
            raise SeeyonReadContractMismatch("The OA workflow section JSON is missing Data.")
        parsed = parse_pending_projection(payload, base_url=final_url or self.base_url)
        if parsed.get("error"):
            raise SeeyonReadContractMismatch(str(parsed["error"]))
        return parsed

    def _fetch_history_page_collection(self, worker, collection: str) -> dict:
        if collection == "tracked":
            return self._read_tracked_page(worker)
        return self._read_history_page(worker, collection)

    def _read_history_page(
        self, worker, collection: str, *, start_date: date | None = None, end_date: date | None = None
    ) -> dict:
        contract = _HISTORY_PAGE_CONTRACTS.get(collection)
        if contract is None:
            raise SeeyonReadContractMismatch(
                f"The OA history page contract does not support {collection}."
            )
        page_url = urljoin(
            self.base_url,
            "collaboration/collaboration.do?"
            + urlencode({"method": contract["method"]}),
        )
        page = worker.goto(page_url)
        if _looks_like_login_url(worker.page_url):
            raise SeeyonLoginRequired(
                f"The central OA session expired while opening the {collection} page."
            )
        try:
            page.wait_for_function(
                "({gridId, managerMethod}) => { const host = document.getElementById(gridId); "
                "const current = window.grid; return Boolean(host && current && current.p && "
                "current.p.managerMethod === managerMethod && "
                "(host.querySelector('td[abbr=\"subject\"]') || Number(current.p.total) === 0)); }",
                arg={
                    "gridId": contract["grid_id"],
                    "managerMethod": contract["manager_method"],
                },
                timeout=12000,
            )
            extracted = read_filtered_history(
                page, collection=collection, start_date=start_date, end_date=end_date
            ) if start_date or end_date else page.evaluate(
                _HISTORY_GRID_EXTRACT_SCRIPT,
                {
                    "gridId": contract["grid_id"],
                    "dateFields": contract["date_fields"],
                },
            )
        except Exception as exc:
            if _looks_like_login_url(worker.page_url):
                raise SeeyonLoginRequired(
                    f"The central OA session expired while loading the {collection} page."
                ) from exc
            raise SeeyonReadContractMismatch(
                f"The OA {collection} page did not expose its workflow grid."
            ) from exc
        if not isinstance(extracted, dict) or not isinstance(extracted.get("items"), list):
            raise SeeyonReadContractMismatch(
                f"The OA {collection} page returned an invalid workflow grid."
            )

        items = []
        for raw_item in extracted["items"]:
            if not isinstance(raw_item, dict):
                continue
            affair_id = str(raw_item.get("affair_id") or "").strip()
            title = str(raw_item.get("title") or "").strip()
            if not affair_id or not title:
                continue
            href = urljoin(
                self.base_url,
                "collaboration/collaboration.do?"
                + urlencode(
                    {
                        "method": "summary",
                        "openFrom": contract["open_from"],
                        "affairId": affair_id,
                        "showTab": "true",
                    }
                ),
            )
            items.append(
                {
                    "index": len(items),
                    "affair_id": affair_id,
                    "title": title,
                    "status": str(raw_item.get("status") or ""),
                    "date": str(raw_item.get("date") or ""),
                    "date_basis": str(raw_item.get("date_basis") or "unknown"),
                    "category": str(raw_item.get("category") or ""),
                    "sender": str(raw_item.get("sender") or ""),
                    "is_track": bool(raw_item.get("is_track")),
                    "raw_text": str(raw_item.get("raw_text") or "")[:800],
                    "href": href,
                }
            )
        return {
            "source": "history_filtered_query" if start_date or end_date else "history_page_grid",
            "count": len(items),
            "total": extracted.get("total"),
            "page": extracted.get("page"),
            "items": items,
            "query_evidence": extracted.get("query_evidence"),
        }

    def _read_tracked_page_fallback(self, worker) -> dict:
        page = worker.goto(
            urljoin(
                self.base_url,
                "portalAffair/portalAffairController.do?"
                + urlencode({"method": "moreTrack"}),
            ),
            timeout_seconds=60,
        )
        if _looks_like_login_url(worker.page_url):
            raise SeeyonLoginRequired(
                "The central OA session expired while opening the tracked page."
            )
        try:
            page.wait_for_function(
                "({gridId}) => { const host = document.getElementById(gridId); "
                "const current = window.grid; return Boolean(host && current && current.p && "
                "(host.querySelector('input[name=\"workitemId\"]') || "
                "Number(current.p.total) === 0)); }",
                arg={"gridId": "gridId"},
                timeout=12000,
            )
            shell = page.evaluate(
                _TRACKED_ID_EXTRACT_SCRIPT,
                {"gridId": "gridId"},
            )
        except Exception as exc:
            if _looks_like_login_url(worker.page_url):
                raise SeeyonLoginRequired(
                    "The central OA session expired while loading the tracked page."
                ) from exc
            raise SeeyonReadContractMismatch(
                "The OA Tracked page did not expose its workflow identifiers."
            ) from exc
        if not isinstance(shell, dict) or not isinstance(shell.get("affair_ids"), list):
            raise SeeyonReadContractMismatch(
                "The OA Tracked page returned invalid workflow identifiers."
            )

        tracked_ids = [str(value).strip() for value in shell["affair_ids"]]
        if not tracked_ids:
            return {
                "total": shell.get("total"),
                "page": shell.get("page"),
                "items": [],
            }

        source_rows = {}
        for collection, open_from in (("sent", "listSent"), ("done", "listDone")):
            collection_result = self._read_history_page(worker, collection)
            for item in collection_result.get("items") or []:
                affair_id = str(item.get("affair_id") or "").strip()
                if affair_id in tracked_ids and affair_id not in source_rows:
                    source_rows[affair_id] = {
                        **item,
                        "open_from": open_from,
                    }

        missing_count = sum(1 for affair_id in tracked_ids if affair_id not in source_rows)
        if missing_count:
            raise SeeyonReadContractMismatch(
                "The OA Tracked identifiers could not be fully reconciled with the "
                f"authoritative Sent and Done grids ({missing_count} unmatched)."
            )
        return {
            "total": shell.get("total"),
            "page": shell.get("page"),
            "items": [source_rows[affair_id] for affair_id in tracked_ids],
        }

    def _read_tracked_page(self, worker) -> dict:
        page = worker.goto(
            urljoin(self.base_url, "main.do?" + urlencode({"method": "main"})),
            timeout_seconds=60,
        )
        if _looks_like_login_url(worker.page_url):
            raise SeeyonLoginRequired(
                "The central OA session expired while opening the tracked page."
            )
        try:
            def click_across_surfaces(selector: str, *, timeout_seconds: float) -> None:
                deadline = time.monotonic() + timeout_seconds
                last_error = None
                while time.monotonic() < deadline:
                    context = getattr(page, "context", None)
                    candidate_pages = list(getattr(context, "pages", []) or [])
                    if page not in candidate_pages:
                        candidate_pages.insert(0, page)
                    for candidate_page in candidate_pages:
                        candidates = [candidate_page]
                        candidates.extend(list(getattr(candidate_page, "frames", []) or []))
                        for candidate in candidates:
                            locator_method = getattr(candidate, "locator", None)
                            if not callable(locator_method):
                                continue
                            try:
                                locator = locator_method(selector)
                                count_method = getattr(locator, "count", None)
                                if callable(count_method) and count_method() < 1:
                                    continue
                                target = locator.first
                                target.wait_for(state="attached", timeout=500)
                                target.click(force=True)
                                return
                            except Exception as exc:
                                last_error = exc
                    page.wait_for_timeout(250)
                if last_error is not None:
                    raise last_error
                raise SeeyonReadContractMismatch(
                    "The OA Tracked page control was not attached."
                )

            click_across_surfaces(
                'li[title*="跟踪"], li:has-text("跟踪事项")',
                timeout_seconds=12,
            )
            click_across_surfaces(
                '[onclick*="method=moreTrack"]',
                timeout_seconds=12,
            )

            tracked_surface = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if _looks_like_login_url(worker.page_url):
                    raise SeeyonLoginRequired(
                        "The central OA session expired while loading the tracked page."
                    )
                context = getattr(page, "context", None)
                candidate_pages = list(getattr(context, "pages", []) or [])
                if page not in candidate_pages:
                    candidate_pages.insert(0, page)
                for candidate_page in candidate_pages:
                    candidates = [candidate_page]
                    candidates.extend(list(getattr(candidate_page, "frames", []) or []))
                    for candidate in candidates:
                        parsed = urlparse(str(getattr(candidate, "url", "") or ""))
                        if (
                            parsed.path.endswith(
                                "/portalAffair/portalAffairController.do"
                            )
                            and parse_qs(parsed.query).get("method") == ["moreTrack"]
                        ):
                            tracked_surface = candidate
                            break
                    if tracked_surface is not None:
                        break
                if tracked_surface is not None:
                    break
                page.wait_for_timeout(250)
            if tracked_surface is None:
                raise SeeyonReadContractMismatch(
                    "The OA Tracked page did not open its moreTrack surface."
                )
            tracked_surface.wait_for_function(
                "({gridId}) => { const host = document.getElementById(gridId); "
                "const current = window.grid; return Boolean(host && current && current.p && "
                "(host.querySelector('td[abbr=\"subject\"]') || Number(current.p.total) === 0)); }",
                arg={"gridId": "gridId"},
                timeout=12000,
            )
            extracted = tracked_surface.evaluate(
                _TRACKED_GRID_EXTRACT_SCRIPT,
                {"gridId": "gridId"},
            )
        except SeeyonLoginRequired:
            raise
        except Exception as exc:
            if _looks_like_login_url(worker.page_url):
                raise SeeyonLoginRequired(
                    "The central OA session expired while loading the tracked page."
                ) from exc
            _LOGGER.info(
                "OA tracked page using identifier reconciliation fallback: %s",
                type(exc).__name__,
            )
            extracted = self._read_tracked_page_fallback(worker)
        if not isinstance(extracted, dict) or not isinstance(extracted.get("items"), list):
            raise SeeyonReadContractMismatch(
                "The OA Tracked page returned an invalid workflow grid."
            )

        items = []
        for raw_item in extracted["items"]:
            if not isinstance(raw_item, dict):
                continue
            affair_id = str(raw_item.get("affair_id") or "").strip()
            title = str(raw_item.get("title") or "").strip()
            if not affair_id or not title:
                continue
            open_from = str(raw_item.get("open_from") or "")
            if open_from not in {"listSent", "listDone"}:
                open_from = "listSent"
            href = urljoin(
                self.base_url,
                "collaboration/collaboration.do?"
                + urlencode(
                    {
                        "method": "summary",
                        "openFrom": open_from,
                        "affairId": affair_id,
                        "showTab": "true",
                    }
                ),
            )
            items.append(
                {
                    "index": len(items),
                    "affair_id": affair_id,
                    "title": title,
                    "status": str(raw_item.get("status") or ""),
                    "date": str(raw_item.get("date") or ""),
                    "category": str(raw_item.get("category") or ""),
                    "raw_text": str(raw_item.get("raw_text") or "")[:800],
                    "href": href,
                }
            )
        return {
            "source": "tracked_page_grid",
            "count": len(items),
            "total": extracted.get("total"),
            "page": extracted.get("page"),
            "items": items,
        }
    def _discover_pending_section_url(
        self,
        worker,
        *,
        timeout_seconds: float = 10,
    ) -> str:
        worker.goto(self.base_url)
        deadline = time.monotonic() + max(timeout_seconds, 0.1)
        while time.monotonic() < deadline:
            if _looks_like_login_url(worker.page_url):
                raise SeeyonLoginRequired("The central OA session expired while opening the home page.")
            section_url = _find_section_resource_url(
                worker.resource_urls(),
                "pendingSection",
            )
            if section_url:
                return _section_url_with_arguments(
                    section_url,
                    {"sectionBeanId": "pendingSection"},
                )
            time.sleep(0.25)
        raise SeeyonReadContractMismatch(
            "The OA home page did not expose the pending section contract in time."
        )

    def _render_workflow_detail(self, worker, *, collection: str, affair_id: str) -> tuple[dict, dict]:
        parsed = self._fetch_workflow_collection(worker, collection)
        source_item = next(
            (
                item
                for item in parsed.get("items") or []
                if str(item.get("affair_id") or "") == affair_id
            ),
            None,
        )
        if source_item is None:
            raise SeeyonReadContractMismatch(
                f"Workflow affair_id was not found in the current {collection} collection."
            )
        return source_item, self._render_workflow_source_detail(worker, source_item)

    def _render_workflow_source_detail(self, worker, source_item: dict) -> dict:
        detail_url = str(source_item.get("href") or "")
        if not detail_url:
            raise SeeyonReadContractMismatch("The selected workflow does not expose a detail page.")
        snapshot = worker.rendered_snapshot(detail_url, settle_ms=1800, include_frames=True)
        final_url = str(snapshot.get("url") or detail_url)
        if _looks_like_login_url(final_url):
            raise SeeyonLoginRequired("The central OA session expired while rendering workflow detail.")
        html_parts = [str(snapshot.get("html") or "")]
        html_parts.extend(
            str(frame.get("html") or "")
            for frame in snapshot.get("frames") or []
            if isinstance(frame, dict)
        )
        parsed_detail = parse_oa_detail("\n".join(html_parts), base_url=final_url)
        return parsed_detail

    def open_login(self, worker) -> None:
        worker.goto(self.base_url)

    def wait_for_login(
        self,
        worker,
        *,
        timeout_seconds: float = 120,
        poll_interval: float = 1,
    ) -> dict:
        self.open_login(worker)
        deadline = time.monotonic() + max(timeout_seconds, 1)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                templates = self.list_templates(worker)
            except (SeeyonLoginRequired, SeeyonSessionCheckUnavailable) as exc:
                last_error = exc
                time.sleep(max(poll_interval, 0.1))
                continue
            return {
                "templates": templates,
                "observed_principal_ref": _principal_from_title(worker.page_title),
                "page_url": worker.page_url,
            }
        raise SeeyonLoginRequired(
            str(last_error) if last_error else "Timed out waiting for the central OA login."
        )


def _find_section_resource_url(resource_urls: list[str], section_bean_id: str) -> str:
    for url in resource_urls:
        parsed = urlparse(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("managerName", [""])[0] != "sectionManager":
            continue
        if query.get("managerMethod", [""])[0] != "doProjection":
            continue
        arguments = _section_arguments(url)
        if arguments.get("sectionBeanId") == section_bean_id:
            return url
    return ""


def _section_arguments(url: str) -> dict:
    query = parse_qs(urlparse(str(url or "")).query, keep_blank_values=True)
    raw_arguments = query.get("arguments", ["{}"])[0] or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    return arguments if isinstance(arguments, dict) else {}


def _section_url_with_arguments(url: str, updates: dict) -> str:
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)
    arguments = _section_arguments(url)
    for key, value in updates.items():
        if value not in (None, ""):
            arguments[key] = str(value)
    query["arguments"] = [json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def _validated_collection(value) -> str:
    if not isinstance(value, str) or value not in _WORKFLOW_COLLECTIONS:
        choices = ", ".join(sorted(_WORKFLOW_COLLECTIONS))
        raise ValueError(f"collection must be one of: {choices}")
    return value


def _validated_internal_collection(value) -> str:
    if not isinstance(value, str) or value not in _INTERNAL_WORKFLOW_COLLECTIONS:
        choices = ", ".join(sorted(_INTERNAL_WORKFLOW_COLLECTIONS))
        raise ValueError(f"internal collection must be one of: {choices}")
    return value


def _validated_identifier(value, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _validated_optional_string(value, name: str, *, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value.strip()


def _validated_optional_date(value, name: str) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a YYYY-MM-DD string")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date") from exc


def _workflow_item_date(value) -> date | None:
    match = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", str(value or ""))
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _validated_integer(value, name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _public_workflow_item(item: dict, collection: str) -> dict:
    public = {
        "affair_id": str(item.get("affair_id") or ""),
        "title": _public_text(item.get("title")),
    }
    if collection == "pending":
        public.update(
            {
                "sender": _public_text(item.get("sender")),
                "date": _public_text(item.get("date")),
                "category": _public_text(item.get("category")),
                "read": bool(item.get("read")),
            }
        )
    else:
        public.update(
            {
                "status": _public_text(item.get("status")),
                "date": _public_text(item.get("date")),
                "category": _public_text(item.get("category")),
            }
        )
    return public


def _public_opinions(value) -> list[dict]:
    opinions = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        public = {
            key: _public_text(item.get(key))
            for key in ("text", "handler", "opinion", "time")
            if item.get(key) not in (None, "")
        }
        if public:
            opinions.append(public)
    return opinions


def _public_text(value) -> str:
    text = re.sub(r"&nbsp;?", " ", str(value or ""), flags=re.IGNORECASE)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_login_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in ("/login", "login.do", "method=login"))


def _looks_like_login_html(value: str) -> bool:
    detector = _LoginFormDetector()
    try:
        detector.feed(str(value or "")[:262_144])
    except Exception:
        return False
    return detector.has_username and detector.has_password


def _safe_response_diagnostics(response: dict) -> str:
    status = int(response.get("status") or 0)
    media_type = str(response.get("content_type") or "unknown")
    media_type = media_type.split(";", 1)[0].strip().lower()
    media_type = re.sub(r"[^a-z0-9.+/-]", "_", media_type)[:80] or "unknown"
    try:
        elapsed_ms = max(0, int(response.get("elapsed_ms") or 0))
    except (TypeError, ValueError):
        elapsed_ms = 0
    return f"HTTP {status}, content_type={media_type}, elapsed_ms={elapsed_ms}"


def _safe_exception_code(exc: Exception) -> str:
    value = re.sub(r"[^A-Z0-9_.-]", "_", exc.__class__.__name__.upper())[:80]
    return value or "REQUEST_ERROR"


class _LoginFormDetector(HTMLParser):
    _USERNAME_NAMES = {"login_username", "loginname", "username"}
    _PASSWORD_NAMES = {"login_password1", "login_password", "password", "pwd"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_username = False
        self.has_password = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = {
            str(name or "").lower(): str(value or "").strip()
            for name, value in attrs
        }
        input_type = values.get("type", "").lower()
        field_name = (values.get("name") or values.get("id") or "").lower()
        autocomplete = values.get("autocomplete", "").lower()
        if input_type == "password" or field_name in self._PASSWORD_NAMES:
            self.has_password = True
        if (
            field_name in self._USERNAME_NAMES
            or autocomplete == "username"
            or input_type == "text"
        ):
            self.has_username = True


def _principal_from_title(title: str) -> str | None:
    match = re.search(r",\s*([^,]+?)\s*,\s*您好", title or "")
    return match.group(1).strip() if match else None


def _find_visible(frames: list, selectors: tuple[str, ...]):
    for frame in frames:
        for selector in selectors:
            locator = frame.locator(selector)
            count = locator.count()
            for index in range(count):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
    return None


def _find_login_form(frames: list):
    for frame in frames:
        username_locator = _find_visible([frame], _USERNAME_SELECTORS)
        password_locator = _find_visible([frame], _PASSWORD_SELECTORS)
        if username_locator is not None and password_locator is not None:
            return frame, username_locator, password_locator
    return None, None, None


def _has_visible(frame, selectors: tuple[str, ...]) -> bool:
    return _find_visible([frame], selectors) is not None
