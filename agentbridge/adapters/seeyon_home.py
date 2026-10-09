from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from typing import Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from agentbridge.adapters.seeyon_write import write_action_risk

TEMPLATE_CENTER_API_URL = (
    "/seeyon/rest/template/myTemplate"
    "?option.n_a_s=1&fragmentId=-6503951670357636432&ordinal=0"
)


_CAP4_SECTION_HEADINGS = (
    "发起者信息",
    "申请人信息",
    "基本信息",
    "申请信息",
    "会议信息",
    "费用信息",
    "出差日程及住宿费",
    "出差补助",
    "在途交通",
    "异地交通",
    "费用统计",
    "账户信息",
    "审批信息",
    "附件信息",
    "差旅报销须知",
)

_CAP4_FIELD_LABELS = (
    "流水号",
    "姓名",
    "工号",
    "部门",
    "申请人",
    "申请部门",
    "申请日期",
    "填报日期",
    "费用归算类型",
    "费用归属部门",
    "结算实体",
    "费用归属事项",
    "财务编号",
    "关联出差申请单",
    "是否申请补助",
    "住宿费小计",
    "在途交通费小计",
    "异地交通费小计",
    "补助小计",
    "应付金额合计",
    "应付金额大写",
    "稽核会计",
    "主管会计",
    "稽核日期",
    "账户类型",
    "收款账户",
    "账户名称",
    "收款账号",
    "银行账户",
    "收款账户开户行",
    "开户银行",
    "备注",
)

_CAP4_TABLE_COLUMNS = {
    "出差日程及住宿费": ("日程及住宿起止日期", "出差城市", "住宿金额", "扣除", "稽核", "电子发票"),
    "在途交通": ("费用日期", "出发地点", "到达地点", "交通方式", "金额", "扣除", "稽核", "电子发票"),
    "异地交通": ("费用日期", "出发地点", "到达地点", "事由", "金额", "扣除", "稽核", "电子发票"),
    "出差补助": ("补助起止日期", "出差补助", "高原补贴", "境外补贴", "补助扣除", "补助稽核", "备注"),
}


_WORKFLOW_OPINION_RE = re.compile(
    r"(?P<handler>\S+)\s+"
    r"(?P<opinion>已阅|同意|不同意|退回|驳回|通过|批准|已处理|阅)\s+"
    r"(?P<time>\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\b"
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    def attr(self, name: str) -> str:
        return self.attrs.get(name, "")

    def has_class(self, class_name: str) -> bool:
        return class_name in self.attr("class").split()

    def text(self) -> str:
        if self.tag in {"script", "style"}:
            return ""
        parts = [*self.text_parts]
        for child in self.children:
            parts.append(child.text())
        return _clean(" ".join(parts), 20000)

    def descendants(self, include_self: bool = False) -> Iterable["_Node"]:
        if include_self:
            yield self
        for child in self.children:
            yield child
            yield from child.descendants()


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].text_parts.append(data)


def parse_pending_list(html: str, *, base_url: str) -> dict:
    root = _parse_html(html)
    section = _find_by_id(root, "section_556815601453123423") or _find_section_by_text(
        root,
        "\u5168\u90e8\u5f85\u529e",
    )
    if section is None:
        return {"count": 0, "items": [], "error": "pending section not found"}

    items = []
    for index, row in enumerate(_find_all(section, tag="tr")):
        cells = [_clean(cell.text(), 300) for cell in _find_all(row, tag="td")]
        compact_cells = [cell for cell in cells if cell]
        link = _find_link(row, onclick_part="checkAndOpenLink") or _first(row, tag="a")
        raw_href = _parse_check_and_open_link(link.attr("onclick") if link else "")
        title_node = _first(row, class_name="titleText")
        title = _clean(
            (title_node.text() if title_node else "")
            or (link.attr("title") if link else "")
            or (compact_cells[0] if compact_cells else "")
            or row.text(),
            500,
        )
        if not title:
            continue
        items.append(
            {
                "index": index,
                "title": title,
                "sender": compact_cells[1] if len(compact_cells) > 1 else "",
                "date": compact_cells[2] if len(compact_cells) > 2 else "",
                "category": compact_cells[3] if len(compact_cells) > 3 else "",
                "affair_id": _query_value(raw_href, "affairId", base_url=base_url),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "read": _has_self_or_descendant_class(row, "AlreadyRead"),
                "raw_text": _clean(row.text(), 800),
            }
        )

    return {"count": len(items), "items": items}


def parse_pending_projection(projection: dict, *, base_url: str) -> dict:
    data = projection.get("Data") if isinstance(projection, dict) else {}
    if not isinstance(data, dict):
        return {"source": "section_api", "count": 0, "items": [], "error": "projection Data not found"}

    items = []
    for index, row in enumerate(data.get("rows") or []):
        cells = row.get("cells") or []
        if not cells:
            continue
        title_cell = cells[0]
        raw_href = title_cell.get("linkURL") or ""
        title = _cell_title(title_cell)
        if not title:
            continue
        items.append(
            {
                "index": index,
                "title": title,
                "sender": _cell_text(cells, 1),
                "date": _cell_text(cells, 2),
                "category": _cell_text(cells, 3),
                "affair_id": _query_value(raw_href, "affairId", base_url=base_url)
                or str(title_cell.get("id") or ""),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "read": title_cell.get("className") != "ReadDifferFromNotRead",
                "raw_text": _clean(" ".join(_cell_text(cells, offset) for offset in range(len(cells))), 800),
            }
        )

    return {
        "source": "section_api",
        "name": projection.get("Name") if isinstance(projection, dict) else "",
        "count": len(items),
        "total": data.get("dataCount") if data.get("dataCount") is not None else data.get("dataNum"),
        "page": data.get("pageNo"),
        "items": items,
    }


def parse_sent_projection(projection: dict, *, base_url: str) -> dict:
    data = projection.get("Data") if isinstance(projection, dict) else {}
    if not isinstance(data, dict):
        return {"source": "section_api", "count": 0, "items": [], "error": "projection Data not found"}

    items = []
    for index, row in enumerate(data.get("rows") or []):
        cells = row.get("cells") or []
        if not cells:
            continue
        title_cell = cells[0]
        raw_href = title_cell.get("linkURL") or ""
        title = _cell_title(title_cell)
        if not title:
            continue
        items.append(
            {
                "index": index,
                "title": title,
                "status": _cell_text(cells, 1),
                "date": _cell_text(cells, 2),
                "category": _cell_text(cells, 3),
                "affair_id": _query_value(raw_href, "affairId", base_url=base_url)
                or str(title_cell.get("id") or ""),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "raw_text": _clean(" ".join(_cell_text(cells, offset) for offset in range(len(cells))), 800),
            }
        )

    return {
        "source": "section_api",
        "name": projection.get("Name") if isinstance(projection, dict) else "",
        "count": len(items),
        "total": data.get("dataCount") if data.get("dataCount") is not None else data.get("dataNum"),
        "page": data.get("pageNo"),
        "items": items,
    }


def parse_template_projection(projection: dict, *, base_url: str) -> dict:
    data = projection.get("Data") if isinstance(projection, dict) else {}
    if not isinstance(data, dict):
        return {"source": "section_api", "count": 0, "items": [], "error": "projection Data not found"}

    items = []
    for index, item in enumerate(data.get("items") or []):
        raw_href = item.get("link") or item.get("linkURL") or ""
        title = _clean(
            _strip_html(item.get("title") or item.get("name") or item.get("subjectHTML") or ""),
            500,
        )
        if not title:
            continue
        items.append(
            {
                "index": index,
                "title": title,
                "template_id": _query_value(raw_href, "templateId", base_url=base_url)
                or str(item.get("id") or item.get("optionId") or ""),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "raw_href": raw_href,
                "open_type": str(item.get("openType") or ""),
                "raw_text": title,
            }
        )

    for row_index, row in enumerate(data.get("rows") or []):
        cells = row.get("cells") or []
        if not cells:
            continue
        title_cell = cells[0]
        cell_html = title_cell.get("cellContentHTML") or ""
        raw_href = title_cell.get("linkURL") or _parse_open_data_link(cell_html)
        title = _clean(
            _strip_html(cell_html)
            or title_cell.get("alt")
            or title_cell.get("title")
            or title_cell.get("name")
            or "",
            500,
        )
        if not title:
            continue
        items.append(
            {
                "index": len(items),
                "title": title,
                "template_id": _query_value(raw_href, "templateId", base_url=base_url)
                or str(title_cell.get("id") or ""),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "raw_href": raw_href,
                "raw_text": _clean(_strip_html(cell_html), 800),
            }
        )

    return {
        "source": "section_api",
        "name": projection.get("Name") if isinstance(projection, dict) else "",
        "count": len(items),
        "total": data.get("dataCount") if data.get("dataCount") is not None else data.get("dataNum"),
        "page": data.get("pageNo"),
        "items": items,
    }


def parse_template_center_response(payload: dict, *, base_url: str) -> dict:
    raw_items = list(_iter_template_center_items(payload))
    items = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_items:
        template_id = _string_value(raw, "id", "templateId", "template_id", "optionId")
        title = _clean(
            _strip_html(
                _string_value(
                    raw,
                    "subject",
                    "templateName",
                    "template_name",
                    "title",
                    "name",
                    "subjectHTML",
                )
            ),
            500,
        )
        if not template_id or not title:
            continue
        dedupe_key = (template_id, title)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        raw_href = _string_value(raw, "href", "link", "linkURL", "url")
        href = _join_app_url(base_url, raw_href) if raw_href else _template_launch_url(base_url, template_id)
        items.append(
            {
                "index": len(items),
                "title": title,
                "subject": title,
                "template_id": template_id,
                "form_app_id": _string_value(raw, "formAppId", "form_app_id"),
                "category_name": _clean(_string_value(raw, "categoryName", "category_name", "category"), 200),
                "category_id": _string_value(raw, "categoryId", "category_id"),
                "module_type": _string_value(raw, "moduleType", "module_type"),
                "body_type": _string_value(raw, "bodyType", "body_type"),
                "href": href,
                "raw_href": raw_href,
                "raw_text": title,
            }
        )

    if not items and isinstance(payload, dict):
        fallback = parse_template_projection(payload, base_url=base_url)
        return {
            **fallback,
            "schema_version": "agentbridge.oa_template_list.v2",
            "source": "template_center_api",
        }

    return {
        "schema_version": "agentbridge.oa_template_list.v2",
        "source": "template_center_api",
        "count": len(items),
        "total": _template_center_total(payload, len(items)),
        "items": items,
    }


def parse_template_list(html: str, *, base_url: str) -> dict:
    root = _parse_html(html)
    section = _find_by_id(root, "section_-6503951670357636432") or _find_section_by_text(
        root,
        "\u6211\u7684\u6a21\u677f",
    )
    if section is None:
        return {"count": 0, "items": [], "error": "template section not found"}

    items = []
    for index, table in enumerate(
        node
        for node in section.descendants()
        if node.tag == "table" and node.has_class("chessboardtable")
    ):
        link = _first(table, tag="a")
        clickable = _find_clickable(table) or link or table
        raw_href = _parse_open_data_link(clickable.attr("onclick"))
        title = _clean(
            table.attr("title")
            or (link.attr("title") if link else "")
            or (link.text() if link else "")
            or table.text(),
            500,
        )
        if not title:
            continue
        items.append(
            {
                "index": index,
                "title": title,
                "template_id": _query_value(raw_href, "templateId", base_url=base_url),
                "href": _join_app_url(base_url, raw_href) if raw_href else "",
                "raw_href": raw_href,
                "raw_text": _clean(table.text(), 800),
            }
        )

    return {"count": len(items), "items": items}


def parse_navigation_inventory(html: str, *, base_url: str) -> dict:
    root = _parse_html(html)
    portals = _parse_portals(root)
    shortcuts = _parse_shortcuts(root, base_url=base_url)
    sections = _parse_sections(root)
    return {
        "portal_count": len(portals),
        "portals": portals,
        "shortcut_count": len(shortcuts),
        "shortcuts": shortcuts,
        "section_count": len(sections),
        "sections": sections,
    }


def extract_history_sections(inventory: dict) -> dict:
    items = []
    sections = inventory.get("sections") if isinstance(inventory, dict) else []
    if not isinstance(sections, list):
        sections = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        tabs = section.get("tabs") if isinstance(section.get("tabs"), list) else []
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            kind = _history_kind_from_name(str(tab.get("name") or ""))
            if not kind:
                continue
            items.append(
                {
                    "index": len(items),
                    "kind": kind,
                    "name": tab.get("name", ""),
                    "section_name": section.get("name", ""),
                    "section_id": section_id,
                    "tab_id": str(tab.get("tab_id") or ""),
                    "count": tab.get("count"),
                    "active": bool(tab.get("active")),
                    "onclick": tab.get("onclick", ""),
                    "section_bean_id": "sentSection",
                }
            )
    return {
        "schema_version": "agentbridge.oa_history_sections.v1",
        "source": "navigation_inventory",
        "count": len(items),
        "items": items,
    }


def parse_oa_detail(html: str, *, base_url: str) -> dict:
    root = _parse_html(html)
    title = _detail_title(root)
    text = _clean(root.text(), 20000)
    fields = _parse_detail_fields(root)
    attachments = _parse_detail_attachments(root, base_url=base_url)
    workflow = _parse_detail_workflow(root)
    actions = _parse_detail_write_actions(html)
    return {
        "title": title,
        "url": base_url,
        "text": text,
        "fields": fields,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "workflow": workflow,
        "workflow_count": len(workflow),
        "actions": actions,
        "action_count": len(actions),
        "write_hints": _parse_detail_write_hints(root, html, base_url=base_url),
    }


def parse_launch_page(html: str, *, base_url: str) -> dict:
    root = _parse_html(html)
    title = _detail_title(root)
    fields = _parse_launch_fields(root)
    hidden_fields = _parse_launch_hidden_fields(root)
    buttons = _parse_launch_buttons(root)
    forms = _parse_launch_forms(root, base_url=base_url)
    actions = _parse_detail_write_actions(html)
    business_form = _parse_cap4_business_form(root)
    return {
        "schema_version": "agentbridge.oa_launch_inspection.v1",
        "title": title,
        "url": base_url,
        "text": _clean(root.text(), 10000),
        "forms": forms,
        "form_count": len(forms),
        "fields": fields,
        "field_count": len(fields),
        "hidden_fields": hidden_fields,
        "hidden_field_count": len(hidden_fields),
        "buttons": buttons,
        "button_count": len(buttons),
        "actions": actions,
        "action_count": len(actions),
        "business_form": business_form,
        "write_hints": _parse_detail_write_hints(root, html, base_url=base_url),
        "safety": {
            "read_only": True,
            "draft_page_opened": True,
            "execute_allowed": False,
            "submitted_count": 0,
        },
    }


def _detail_title(root: _Node) -> str:
    for node in root.descendants():
        if node.tag in {"h1", "h2"}:
            title = _clean(node.text(), 300)
            if title:
                return title
    for element_id in ("summarySubject", "subject", "title"):
        node = _find_by_id(root, element_id)
        if node:
            title = _clean(node.text() or node.attr("title"), 300)
            if title:
                return title
    title_node = _first(root, tag="title")
    return _clean(title_node.text(), 300) if title_node else ""


def _parse_detail_fields(root: _Node) -> list[dict]:
    fields = []
    seen = set()
    for row in _find_all(root, tag="tr"):
        row_marker = " ".join(row.attr(name).lower() for name in ("id", "class", "name") if row.attr(name))
        if any(marker in row_marker for marker in ("workflow", "process", "opinion", "processlog")):
            continue
        cells = [
            _clean(cell.text(), 500)
            for cell in row.children
            if cell.tag in {"th", "td"}
        ]
        cells = [cell for cell in cells if cell]
        if len(cells) < 2:
            continue
        name = _field_name(cells[0])
        value = _clean(" ".join(cells[1:]), 1000)
        if name.lower() in {"node", "opinion", "workflow", "process"}:
            continue
        if not name or not value:
            continue
        key = (name, value)
        if key in seen:
            continue
        seen.add(key)
        fields.append({"name": name, "value": value})
        if len(fields) >= 200:
            break
    return fields


def _parse_launch_forms(root: _Node, *, base_url: str) -> list[dict]:
    forms = []
    for index, form in enumerate(_find_all(root, tag="form")):
        field_count = 0
        hidden_count = 0
        button_count = 0
        for node in form.descendants():
            if _is_launch_field(node):
                if node.tag == "input" and node.attr("type").lower() == "hidden":
                    hidden_count += 1
                else:
                    field_count += 1
            if _is_launch_button(node):
                button_count += 1
        action = form.attr("action")
        forms.append(
            {
                "index": index,
                "id": form.attr("id"),
                "name": form.attr("name"),
                "method": (form.attr("method") or "GET").upper(),
                "action": _join_app_url(base_url, action) if action else "",
                "field_count": field_count,
                "hidden_field_count": hidden_count,
                "button_count": button_count,
            }
        )
    return forms


def _parse_launch_fields(root: _Node) -> list[dict]:
    labels = _launch_label_index(root)
    fields = []
    seen = set()
    for node in root.descendants():
        if not _is_launch_field(node):
            continue
        if node.tag == "input" and node.attr("type").lower() == "hidden":
            continue
        name = node.attr("name") or node.attr("id")
        field_id = node.attr("id")
        key = (node.tag, name, field_id)
        if key in seen:
            continue
        seen.add(key)
        field = {
            "tag": node.tag,
            "type": _launch_field_type(node),
            "name": name,
            "id": field_id,
            "label": _launch_field_label(node, labels),
            "required": "required" in node.attrs,
            "readonly": "readonly" in node.attrs,
            "disabled": "disabled" in node.attrs,
            "value_present": bool(node.attr("value")),
        }
        if node.tag == "select":
            field["options_count"] = len(_find_all(node, tag="option"))
        fields.append(field)
        if len(fields) >= 300:
            break
    return fields


def _parse_launch_hidden_fields(root: _Node) -> list[dict]:
    hidden_fields = []
    seen = set()
    for node in root.descendants():
        if node.tag != "input" or node.attr("type").lower() != "hidden":
            continue
        name = node.attr("name") or node.attr("id")
        if not name or name in seen:
            continue
        seen.add(name)
        hidden_fields.append(
            {
                "name": name,
                "id": node.attr("id"),
                "value_present": bool(node.attr("value")),
            }
        )
        if len(hidden_fields) >= 200:
            break
    return hidden_fields


def _parse_launch_buttons(root: _Node) -> list[dict]:
    buttons = []
    seen = set()
    for node in root.descendants():
        if not _is_launch_button(node):
            continue
        text = _clean(node.text() or node.attr("value") or node.attr("title") or node.attr("aria-label"), 200)
        code = _clean(node.attr("id") or node.attr("name") or text, 100)
        risk = write_action_risk(code, text)
        action_like = risk == "high" or _launch_button_type(node) == "submit" or _looks_like_write_button(code, text)
        key = (node.tag, code, text)
        if key in seen:
            continue
        seen.add(key)
        buttons.append(
            {
                "tag": node.tag,
                "type": _launch_button_type(node),
                "id": node.attr("id"),
                "name": node.attr("name"),
                "text": text,
                "risk": risk,
                "action_like": action_like,
                "requires_confirmation": action_like,
            }
        )
        if len(buttons) >= 200:
            break
    return buttons


def _parse_cap4_business_form(root: _Node) -> dict:
    text = _clean(root.text(), 20000)
    empty = _empty_business_form()
    if "cap4" not in text.lower():
        return empty

    title = _cap4_title(text)
    section_positions = _cap4_section_positions(text)
    sections = [section for _, section in section_positions]
    field_candidates = _cap4_field_candidates(text, section_positions)
    table_candidates = _cap4_table_candidates(text)
    if not title and not sections and not field_candidates and not table_candidates:
        return empty
    return {
        "detected": True,
        "source": "cap4_text",
        "title": title,
        "sections": sections,
        "section_count": len(sections),
        "field_candidates": field_candidates,
        "field_count": len(field_candidates),
        "table_candidates": table_candidates,
        "table_count": len(table_candidates),
    }


def _empty_business_form() -> dict:
    return {
        "detected": False,
        "source": "",
        "title": "",
        "sections": [],
        "section_count": 0,
        "field_candidates": [],
        "field_count": 0,
        "table_candidates": [],
        "table_count": 0,
    }


def _cap4_title(text: str) -> str:
    tokens = _cap4_token_positions(text)
    for index, (_, token) in enumerate(tokens):
        if token.lower() == "cap4" and index + 1 < len(tokens):
            candidate = tokens[index + 1][1]
            if candidate and candidate.lower() != "cap4":
                return _clean(candidate, 200)
    return ""


def _cap4_section_positions(text: str) -> list[tuple[int, str]]:
    tokens = _cap4_token_positions(text)
    token_positions = {}
    for position, token in tokens:
        token_positions.setdefault(token, position)
    positions = []
    seen = set()
    for heading in _CAP4_SECTION_HEADINGS:
        position = token_positions.get(heading)
        if position is None:
            position = text.find(heading)
        if position < 0 or heading in seen:
            continue
        seen.add(heading)
        positions.append((position, heading))
    positions.sort(key=lambda item: item[0])
    return positions


def _cap4_field_candidates(text: str, section_positions: list[tuple[int, str]]) -> list[dict]:
    label_set = set(_CAP4_FIELD_LABELS)
    matches = []
    seen = set()
    for position, token in _cap4_token_positions(text):
        if token not in label_set or token in seen:
            continue
        seen.add(token)
        matches.append(
            {
                "position": position,
                "label": token,
                "section": _cap4_section_for_position(position, section_positions),
                "source": "cap4_text",
                "writable": None,
            }
        )
        if len(matches) >= 200:
            break
    matches.sort(key=lambda item: item["position"])
    for candidate in matches:
        del candidate["position"]
    return matches


def _cap4_table_candidates(text: str) -> list[dict]:
    token_values = {token for _, token in _cap4_token_positions(text)}
    tables = []
    for name, known_columns in _CAP4_TABLE_COLUMNS.items():
        if name not in token_values and text.find(name) < 0:
            continue
        columns = [column for column in known_columns if column in token_values or text.find(column) >= 0]
        if not columns:
            continue
        tables.append(
            {
                "name": name,
                "columns": columns,
                "column_count": len(columns),
                "source": "cap4_text",
            }
        )
    return tables


def _cap4_section_for_position(position: int, section_positions: list[tuple[int, str]]) -> str:
    current = ""
    for section_position, section in section_positions:
        if section_position > position:
            break
        current = section
    return current


def _cap4_token_positions(text: str) -> list[tuple[int, str]]:
    tokens = []
    cursor = 0
    for raw_token in text.split():
        position = text.find(raw_token, cursor)
        if position < 0:
            position = cursor
        cursor = position + len(raw_token)
        token = raw_token.strip(" \t\r\n:：；;，,。.*")
        if token:
            tokens.append((position, token))
    return tokens


def _launch_label_index(root: _Node) -> dict[str, str]:
    labels = {}
    for label in _find_all(root, tag="label"):
        text = _clean(label.text(), 200)
        if not text:
            continue
        target = label.attr("for")
        if target:
            labels[target] = text
    return labels


def _launch_field_label(node: _Node, labels: dict[str, str]) -> str:
    for key in (node.attr("id"), node.attr("name")):
        if key and labels.get(key):
            return labels[key]
    return _clean(node.attr("title") or node.attr("placeholder") or node.attr("aria-label"), 200)


def _is_launch_field(node: _Node) -> bool:
    if node.tag in {"select", "textarea"}:
        return True
    return node.tag == "input" and node.attr("type").lower() not in {"button", "submit", "reset", "image"}


def _launch_field_type(node: _Node) -> str:
    if node.tag == "input":
        return node.attr("type").lower() or "text"
    return node.tag


def _is_launch_button(node: _Node) -> bool:
    if node.tag == "button":
        return True
    if node.tag == "input" and node.attr("type").lower() in {"button", "submit", "reset", "image"}:
        return True
    if node.tag != "a":
        return False
    marker = " ".join(
        node.attr(name).lower()
        for name in ("class", "role", "onclick", "href")
        if node.attr(name)
    )
    return any(value in marker for value in ("button", "btn", "submit", "save", "delete", "archive"))


def _launch_button_type(node: _Node) -> str:
    if node.tag == "input":
        return node.attr("type").lower() or "button"
    if node.tag == "button":
        return node.attr("type").lower() or "button"
    return "link"


def _looks_like_write_button(code: str, text: str) -> bool:
    value = f"{code} {text}".lower()
    return any(
        marker in value
        for marker in (
            "submit",
            "send",
            "approve",
            "agree",
            "reject",
            "return",
            "archive",
            "delete",
            "revoke",
            "upload",
            "continuesubmit",
        )
    )


def _field_name(value: str) -> str:
    return _clean(str(value or "").rstrip(":："), 200)


def _parse_detail_attachments(root: _Node, *, base_url: str) -> list[dict]:
    attachments = []
    seen = set()
    for link in _find_all(root, tag="a"):
        href = link.attr("href").replace("&amp;", "&")
        name = _clean(link.attr("title") or link.text() or href.rsplit("/", 1)[-1], 300)
        if not href or not _looks_like_attachment(href, name):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        attachments.append(
            {
                "name": name,
                "href": absolute,
                "raw_href": href,
            }
        )
    return attachments


def _looks_like_attachment(href: str, name: str) -> bool:
    value = f"{href} {name}".lower()
    if any(marker in value for marker in ("download", "fileupload", "attachment", "fileid=", "createfile.do")):
        return True
    return bool(re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|png|jpe?g)(\?|$)", value))


def _parse_detail_workflow(root: _Node) -> list[dict]:
    workflow = []
    seen = set()
    extra_keywords = ("意见", "流程", "节点", "处理", "审批", "已阅")
    keywords = ("意见", "流程", "节点", "处理", "审批", "opinion", "workflow", "process", "approved", "approval")
    for node in root.descendants():
        if node.tag in {"html", "body", "document"}:
            continue
        if node.tag not in {"tr", "li", "div", "section", "p"}:
            continue
        marker = " ".join(
            node.attr(name).lower()
            for name in ("id", "class", "name")
            if node.attr(name)
        )
        text = _clean(node.text(), 1000)
        if not text:
            continue
        haystack = f"{marker} {text}".lower()
        if not any(keyword.lower() in haystack for keyword in (*keywords, *extra_keywords)):
            continue
        for entry in _clean_workflow_entries(text):
            key = _workflow_entry_key(entry)
            if key in seen:
                continue
            seen.add(key)
            workflow.append(entry)
            if len(workflow) >= 100:
                break
        if len(workflow) >= 100:
            break
    return _remove_workflow_ancestor_text(workflow)


def _remove_workflow_ancestor_text(workflow: list[dict]) -> list[dict]:
    structured = [
        item
        for item in workflow
        if all(item.get(name) for name in ("handler", "opinion", "time"))
    ]
    if not structured:
        return workflow
    ancestor_markers = ("审批信息", "审批意见", "流程处理意见", "处理人意见区")
    filtered = []
    for item in workflow:
        if item in structured:
            filtered.append(item)
            continue
        text = _clean(str(item.get("text") or ""), 1000)
        if any(marker in text for marker in ancestor_markers) and any(
            str(entry["handler"]) in text
            and str(entry["opinion"]) in text
            and str(entry["time"]).split()[0] in text
            for entry in structured
        ):
            continue
        filtered.append(item)
    return filtered


def _clean_workflow_entries(text: str) -> list[dict]:
    text = _clean(text, 1000)
    if not text or _looks_like_workflow_noise(text):
        return []
    text = _strip_workflow_ui_tail(text)
    if not text or _looks_like_workflow_noise(text):
        return []
    structured = [_workflow_entry_from_match(match) for match in _WORKFLOW_OPINION_RE.finditer(text)]
    if structured:
        return structured
    return [{"text": text}]


def _clean_workflow_entry(text: str) -> dict | None:
    entries = _clean_workflow_entries(text)
    return entries[0] if entries else None


def _parse_workflow_entry_fields(text: str) -> dict[str, str]:
    match = _WORKFLOW_OPINION_RE.match(text)
    if not match:
        return {}
    return _workflow_fields_from_match(match)


def _workflow_entry_from_match(match: re.Match) -> dict[str, str]:
    fields = _workflow_fields_from_match(match)
    return {
        "text": f"{fields['handler']} {fields['opinion']} {fields['time']}",
        **fields,
    }


def _workflow_fields_from_match(match: re.Match) -> dict[str, str]:
    return {
        "handler": match.group("handler"),
        "opinion": match.group("opinion"),
        "time": match.group("time"),
    }


def _workflow_entry_key(entry: dict) -> tuple:
    if all(entry.get(key) for key in ("handler", "opinion", "time")):
        return (entry.get("handler"), entry.get("opinion"), entry.get("time"))
    return (entry.get("text", ""),)


def _looks_like_workflow_noise(text: str) -> bool:
    compact = _clean(text, 1000)
    lower = compact.lower()
    code_markers = (
        "<!--",
        "{{",
        "function ",
        "var ",
        "jsonarrbase",
        "document.",
        "return false",
        "$.",
        "$(",
        ".css(",
    )
    if any(marker in lower for marker in code_markers):
        return True
    header_markers = (
        "处理人意见区",
        "与我相关",
        "意见隐藏",
        "显示更多意见",
        "不包括:",
        "不包括：",
        "关联文档",
        "明细日志",
        "流程最大化",
    )
    has_timestamp = bool(re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}", compact))
    has_english_opinion = bool(re.search(r"\b(opinion|approved|approval|rejected|reject)\b", lower))
    if any(marker in compact for marker in header_markers):
        return True
    chinese_opinion_markers = (
        "已阅",
        "同意",
        "不同意",
        "退回",
        "驳回",
        "通过",
        "批准",
        "已处理",
        "阅",
    )
    has_chinese_opinion = has_timestamp and any(marker in compact for marker in chinese_opinion_markers)
    if not has_english_opinion and not has_chinese_opinion:
        return True
    return False


def _strip_workflow_ui_tail(text: str) -> str:
    text = re.sub(r"\s+暂无数据\s*$", "", text)
    text = re.sub(r"\s+回复\s*\(\s*\)\s*\d+\s*$", "", text)
    return _clean(text, 1000)


def _parse_detail_write_actions(html: str) -> list[dict]:
    actions = []
    seen = set()
    for item in _extract_json_arr_base_items(html):
        if not isinstance(item, dict):
            continue
        codes = item.get("codes") if isinstance(item.get("codes"), list) else []
        code = str((codes[0] if codes else item.get("id")) or "").strip()
        label = _clean(str(item.get("label") or code), 100)
        if not code and not label:
            continue
        key = (code, label)
        if key in seen:
            continue
        seen.add(key)
        actions.append(
            {
                "code": code,
                "label": label,
                "id": str(item.get("id") or code),
                "access": "write",
                "risk": write_action_risk(code, label),
                "requires_confirmation": True,
                "supports_dry_run": True,
                "source": "jsonArrBase",
            }
        )
    return actions


def _extract_json_arr_base_items(html: str) -> list:
    items = []
    for match in re.finditer(r"jsonArrBase\s*=\s*'((?:\\'|[^'])*)'", str(html or "")):
        raw_json = match.group(1).replace("\\'", "'").replace('\\"', '"')
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, list):
            items.extend(decoded)
    return items


def _parse_detail_write_hints(root: _Node, html: str, *, base_url: str) -> dict:
    csrf_tokens = []
    if re.search(r"\bCSRFTOKEN\b\s*=", str(html or "")):
        csrf_tokens.append({"name": "CSRFTOKEN", "value_present": True})
    hidden_fields = []
    seen = set()
    for node in root.descendants():
        if node.tag != "input" or node.attr("type").lower() != "hidden":
            continue
        name = node.attr("name") or node.attr("id")
        if not name or name in seen:
            continue
        seen.add(name)
        hidden_fields.append({"name": name, "value_present": bool(node.attr("value"))})
        if len(hidden_fields) >= 100:
            break
    hints = {"csrf_tokens": csrf_tokens, "hidden_fields": hidden_fields}
    endpoint_candidates = _parse_write_endpoint_candidates(str(html or ""), base_url=base_url)
    if endpoint_candidates:
        hints["endpoint_candidates"] = endpoint_candidates
    return hints


def _parse_write_endpoint_candidates(html: str, *, base_url: str) -> list[dict]:
    candidates = []
    seen = set()
    for match in re.finditer(r"['\"]([^'\"]+\.do\?method=[^'\"]+)['\"]", html):
        raw_url = match.group(1).replace("&amp;", "&")
        if not _looks_like_write_endpoint(raw_url):
            continue
        absolute = _join_app_url(base_url, raw_url)
        if absolute in seen:
            continue
        seen.add(absolute)
        candidates.append(
            {
                "url": absolute,
                "method": "UNKNOWN",
                "risk": "high",
                "source": "rendered_html",
                "tested": False,
            }
        )
        if len(candidates) >= 50:
            break
    return candidates


def _looks_like_write_endpoint(url: str) -> bool:
    parsed = urlparse(_join_app_url("http://localhost/seeyon/main.do?method=main", str(url or "")))
    method_values = parse_qs(parsed.query, keep_blank_values=True).get("method", [])
    value = f"{parsed.path} {' '.join(method_values)}".lower()
    return any(
        marker in value
        for marker in (
            "submit",
            "finish",
            "save",
            "archive",
            "delete",
            "revoke",
            "cancel",
            "upload",
            "opinion",
            "workitem",
        )
    )


def _parse_portals(root: _Node) -> list[dict]:
    portals = []
    for index, node in enumerate(
        node
        for node in root.descendants()
        if node.tag == "li" and node.attr("id").startswith("spaceLi_")
    ):
        name = _clean(node.attr("title") or node.text(), 100)
        if not name:
            continue
        portals.append(
            {
                "index": index,
                "name": name,
                "portal_id": node.attr("id").removeprefix("spaceLi_"),
                "navigation_index": _parse_navigation_index(node.attr("onclick")),
                "active": node.has_class("current"),
                "onclick": node.attr("onclick"),
            }
        )
    return portals


def _parse_shortcuts(root: _Node, *, base_url: str) -> list[dict]:
    shortcuts = []
    for index, node in enumerate(
        node
        for node in root.descendants()
        if node.has_class("lev1Title")
        and node.has_class("navTitleName")
        and "onSeeyonTopNavMenuClick" in node.attr("onclick")
    ):
        args = _parse_single_quoted_args(node.attr("onclick"))
        raw_href = args[0] if len(args) > 0 else ""
        target = args[2] if len(args) > 2 else ""
        name = _clean(node.attr("title") or node.text(), 100)
        if not name:
            continue
        shortcuts.append(
            {
                "index": index,
                "name": name,
                "menu_id": args[1] if len(args) > 1 else "",
                "target": target,
                "resource_code": args[3] if len(args) > 3 else "",
                "raw_href": raw_href.replace("&amp;", "&"),
                "href": urljoin(base_url, raw_href.replace("&amp;", "&")) if raw_href else "",
                "opens_new_window": target == "newWindow",
                "onclick": node.attr("onclick"),
            }
        )
    return shortcuts


def _parse_sections(root: _Node) -> list[dict]:
    sections = []
    for section in (
        node
        for node in root.descendants()
        if node.attr("id").startswith("section_") and node.attr("id") != "section_"
    ):
        tabs = []
        for index, tab in enumerate(
            node
            for node in section.descendants()
            if node.tag == "li" and node.attr("id").startswith("sectionName_")
        ):
            visible_name, visible_count = _parse_counted_label(tab.text())
            title_name, title_count = _parse_counted_label(tab.attr("title"))
            name = title_name or visible_name
            count = visible_count if visible_count is not None else title_count
            if not name:
                continue
            tabs.append(
                {
                    "index": index,
                    "name": name,
                    "count": count,
                    "tab_id": tab.attr("id").removeprefix("sectionName_"),
                    "active": tab.has_class("current"),
                    "onclick": tab.attr("onclick"),
                }
            )
        if tabs:
            sections.append(
                {
                    "index": len(sections),
                    "section_id": section.attr("id").removeprefix("section_"),
                    "name": tabs[0]["name"],
                    "tabs": tabs,
                }
            )
    return sections


def _history_kind_from_name(name: str) -> str:
    compact = _clean(str(name or ""), 100).replace(" ", "")
    lowered = compact.lower()
    if not compact:
        return ""
    if "\u5df2\u53d1" in compact or lowered in {"sent", "listsent"}:
        return "sent"
    if "\u5df2\u529e" in compact or lowered in {"done", "finished", "processed"}:
        return "done"
    if "\u8ddf\u8e2a" in compact or lowered in {"tracked", "followed", "tracking"}:
        return "tracked"
    return ""


def _iter_template_center_items(value) -> Iterable[dict]:
    if isinstance(value, dict):
        if _looks_like_template_center_item(value):
            yield value
        for child in value.values():
            yield from _iter_template_center_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_template_center_items(child)


def _looks_like_template_center_item(value: dict) -> bool:
    if not isinstance(value, dict):
        return False
    has_id = any(value.get(key) not in (None, "") for key in ("id", "templateId", "template_id", "optionId"))
    has_title = any(
        value.get(key) not in (None, "")
        for key in ("subject", "templateName", "template_name", "title", "name", "subjectHTML")
    )
    has_template_metadata = any(
        key in value
        for key in (
            "formAppId",
            "form_app_id",
            "categoryName",
            "category_name",
            "moduleType",
            "module_type",
            "bodyType",
            "body_type",
        )
    )
    return has_id and has_title and has_template_metadata


def _string_value(source: dict, *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _template_launch_url(base_url: str, template_id: str) -> str:
    raw_href = (
        "/collaboration/collaboration.do?method=newColl&from=templateNewColl"
        f"&templateId={template_id}&showTab=true"
    )
    return _join_app_url(base_url, raw_href)


def _template_center_total(payload: dict, fallback: int) -> int:
    if not isinstance(payload, dict):
        return fallback
    for key in ("total", "count", "dataCount", "dataNum"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("total", "count", "dataCount", "dataNum"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return fallback


def _parse_html(html: str) -> _Node:
    parser = _TreeBuilder()
    parser.feed(_normalize_html_input(html))
    parser.close()
    return parser.root


def _normalize_html_input(html: str) -> str:
    html = str(html or "")
    stripped = html.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return html
        if isinstance(decoded, str):
            return decoded
    return html


def _clean(value: str, max_length: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_length]


def _cell_text(cells: list[dict], index: int) -> str:
    if index >= len(cells):
        return ""
    cell = cells[index] or {}
    return _clean(_strip_html(cell.get("cellContentHTML") or cell.get("cellContent") or cell.get("alt") or ""), 300)


def _cell_title(cell: dict) -> str:
    return _clean(
        _strip_html(
            cell.get("cellContentHTML")
            or cell.get("cellContent")
            or cell.get("alt")
            or cell.get("title")
            or ""
        ),
        500,
    )


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", str(value or ""))


def _find_by_id(root: _Node, element_id: str) -> _Node | None:
    return next((node for node in root.descendants(include_self=True) if node.attr("id") == element_id), None)


def _find_section_by_text(root: _Node, text: str) -> _Node | None:
    return next(
        (
            node
            for node in root.descendants()
            if node.tag in {"div", "section"}
            and (node.has_class("sectionPanel") or node.attr("id").startswith("section_"))
            and text in node.text()
        ),
        None,
    )


def _find_all(root: _Node, *, tag: str | None = None, class_name: str | None = None) -> list[_Node]:
    return [
        node
        for node in root.descendants()
        if (tag is None or node.tag == tag) and (class_name is None or node.has_class(class_name))
    ]


def _first(root: _Node, *, tag: str | None = None, class_name: str | None = None) -> _Node | None:
    return next(iter(_find_all(root, tag=tag, class_name=class_name)), None)


def _find_link(root: _Node, *, onclick_part: str) -> _Node | None:
    return next(
        (
            node
            for node in root.descendants()
            if node.tag == "a" and onclick_part in node.attr("onclick")
        ),
        None,
    )


def _find_clickable(root: _Node) -> _Node | None:
    return next((node for node in root.descendants() if node.attr("onclick")), None)


def _has_self_or_descendant_class(root: _Node, class_name: str) -> bool:
    return any(node.has_class(class_name) for node in root.descendants(include_self=True))


def _parse_check_and_open_link(onclick: str) -> str:
    match = re.search(r"checkAndOpenLink\('([^']+)'", onclick or "")
    return match.group(1).replace("&amp;", "&") if match else ""


def _parse_open_data_link(onclick: str) -> str:
    match = re.search(r"['\"]url['\"]\s*:\s*['\"]([^'\"]+)", onclick or "")
    return match.group(1).replace("&amp;", "&") if match else ""


def _parse_navigation_index(onclick: str) -> str:
    match = re.search(r"showNavigation\(([^,\)]+)", onclick or "")
    return match.group(1).strip("'\" ") if match else ""


def _parse_single_quoted_args(source: str) -> list[str]:
    return [match.replace("&amp;", "&") for match in re.findall(r"'([^']*)'", source or "")]


def _parse_counted_label(label: str) -> tuple[str, int | None]:
    label = _clean(label, 100)
    match = re.match(r"^(.*?)[(（](\d+)[)）]$", label)
    if not match:
        return label, None
    return _clean(match.group(1), 100), int(match.group(2))


def _query_value(url: str, key: str, *, base_url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(_join_app_url(base_url, url))
    values = parse_qs(parsed.query, keep_blank_values=True).get(key)
    if values:
        return values[0]
    match = re.search(rf"[?&]{re.escape(key)}=([^&]+)", url)
    return unquote(match.group(1)) if match else ""


def _join_app_url(base_url: str, url: str) -> str:
    url = str(url or "").replace("&amp;", "&")
    if not url:
        return ""
    base = urlparse(base_url)
    if (
        base.path.startswith("/seeyon/")
        and url.startswith("/")
        and not url.startswith("/seeyon/")
        and not url.startswith("//")
    ):
        return urljoin(base_url, f"/seeyon{url}")
    return urljoin(base_url, url)
