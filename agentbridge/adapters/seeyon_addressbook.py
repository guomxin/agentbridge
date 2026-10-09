from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import urlencode, urljoin, urlparse

from agentbridge.adapters.base import AdapterLoginRequired


ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY = "oa.addressbook.organization.tree"
ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY = "oa.addressbook.department.members"
ADDRESSBOOK_PERSON_SEARCH_CAPABILITY = "oa.addressbook.person.search"
ADDRESSBOOK_PERSON_GET_CAPABILITY = "oa.addressbook.person.get"
ADDRESSBOOK_GROUP_LIST_CAPABILITY = "oa.addressbook.group.list"
ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY = "oa.addressbook.group.members"
ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY = (
    "oa.addressbook.private_contact.search"
)
ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY = "oa.addressbook.private_contact.get"
ADDRESSBOOK_EXPORT_CAPABILITY = "oa.addressbook.export"

ADDRESSBOOK_CAPABILITIES = frozenset(
    {
        ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY,
        ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY,
        ADDRESSBOOK_PERSON_SEARCH_CAPABILITY,
        ADDRESSBOOK_PERSON_GET_CAPABILITY,
        ADDRESSBOOK_GROUP_LIST_CAPABILITY,
        ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY,
        ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY,
        ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY,
        ADDRESSBOOK_EXPORT_CAPABILITY,
    }
)

ADDRESSBOOK_GROUP_TYPES = ("private", "personal", "system", "project")
ADDRESSBOOK_EXPORT_SOURCES = (
    "person_search",
    "department_members",
    "group_members",
    "private_contacts",
)
ADDRESSBOOK_EXPORT_MAX_ROWS = 500

_COMMON_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}
_SEARCH_TYPE = {"type": "string", "enum": ["name", "all"]}
_GROUP_TYPE = {"type": "string", "enum": list(ADDRESSBOOK_GROUP_TYPES)}

ADDRESSBOOK_INPUT_SCHEMAS = {
    ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY: {
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "limit": _COMMON_LIMIT,
        },
        "additionalProperties": False,
    },
    ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY: {
        "type": "object",
        "properties": {
            "department_id": {"type": "string"},
            "keyword": {"type": "string"},
            "search_type": _SEARCH_TYPE,
            "include_descendants": {"type": "boolean"},
            "limit": _COMMON_LIMIT,
        },
        "required": ["department_id"],
        "additionalProperties": False,
    },
    ADDRESSBOOK_PERSON_SEARCH_CAPABILITY: {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "search_type": _SEARCH_TYPE,
            "limit": _COMMON_LIMIT,
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    ADDRESSBOOK_PERSON_GET_CAPABILITY: {
        "type": "object",
        "properties": {"person_ref": {"type": "string"}},
        "required": ["person_ref"],
        "additionalProperties": False,
    },
    ADDRESSBOOK_GROUP_LIST_CAPABILITY: {
        "type": "object",
        "properties": {
            "group_type": _GROUP_TYPE,
            "keyword": {"type": "string"},
            "limit": _COMMON_LIMIT,
        },
        "additionalProperties": False,
    },
    ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY: {
        "type": "object",
        "properties": {
            "group_type": _GROUP_TYPE,
            "group_id": {"type": "string"},
            "keyword": {"type": "string"},
            "search_type": _SEARCH_TYPE,
            "limit": _COMMON_LIMIT,
        },
        "required": ["group_type", "group_id"],
        "additionalProperties": False,
    },
    ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY: {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "search_type": _SEARCH_TYPE,
            "group_id": {"type": "string"},
            "limit": _COMMON_LIMIT,
        },
        "additionalProperties": False,
    },
    ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY: {
        "type": "object",
        "properties": {"contact_ref": {"type": "string"}},
        "required": ["contact_ref"],
        "additionalProperties": False,
    },
    ADDRESSBOOK_EXPORT_CAPABILITY: {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": list(ADDRESSBOOK_EXPORT_SOURCES)},
            "query": {"type": "string"},
            "search_type": _SEARCH_TYPE,
            "department_id": {"type": "string"},
            "include_descendants": {"type": "boolean"},
            "group_type": _GROUP_TYPE,
            "group_id": {"type": "string"},
            "limit": _COMMON_LIMIT,
        },
        "required": ["source"],
        "additionalProperties": False,
    },
}


class SeeyonAddressbookContractMismatch(RuntimeError):
    pass


_TREE_READINESS_SCRIPT = r"""
({treeId}) => ({
  documentState: document.readyState,
  jqueryPresent: Boolean(window.jQuery),
  pluginPresent: Boolean(window.jQuery?.fn?.zTree),
  treePresent: Boolean(window.jQuery?.fn?.zTree?.getZTreeObj?.(treeId))
})
"""


def _organization_nodes(page, tree_frame):
    """Bounded DOM readiness polling; never navigate or replay a business call."""
    started = time.monotonic()
    observations = []
    for attempt in range(4):
        _assert_not_login(str(getattr(page, "url", "")))
        _assert_not_login(str(getattr(tree_frame, "url", "")))
        try:
            raw = tree_frame.evaluate(_TREE_READINESS_SCRIPT, {"treeId": "accountTree"})
            if not isinstance(raw, dict):
                raise ValueError("invalid readiness contract")
            snapshot = {
                "stage": "tree_object", "frameReady": True,
                "documentState": raw.get("documentState") if raw.get("documentState") in ("loading", "interactive", "complete") else "unknown",
                **{k: raw.get(k) is True for k in ("jqueryPresent", "pluginPresent", "treePresent")},
                "elapsedMs": round((time.monotonic() - started) * 1000),
            }
            observations.append(snapshot)
            if snapshot["treePresent"]:
                nodes = tree_frame.evaluate(_TREE_SCRIPT, {"treeId": "accountTree"})
                if not isinstance(nodes, list) or any(
                    not isinstance(n, dict) or not str(n.get('id') or '').strip()
                    or not str(n.get('name') or '').strip() or not isinstance(n.get('path'),list)
                    for n in nodes
                ):
                    code = "OA_ORG_TREE_PARSE_FAILED"
                elif not nodes:
                    code = "OA_ORG_TREE_EMPTY_VISIBILITY_UNDETERMINED"
                else:
                    return nodes, observations
                break
        except Exception as exc:
            # Do not copy browser exception text, URLs, credentials, or DOM.
            _assert_not_login(str(getattr(page, "url", "")))
            _assert_not_login(str(getattr(tree_frame, "url", "")))
            code = "OA_ORG_TREE_EVALUATION_FAILED"
            break
        if attempt < 3:
            page.wait_for_timeout(250)
    else:
        code = ("OA_ORG_TREE_PAGE_NOT_READY" if observations[-1]["documentState"] != "complete"
                else "OA_ORG_TREE_OBJECT_MISSING")
    raise SeeyonAddressbookContractMismatch(json.dumps({
        "code": code, "observations": observations, "readAttempts": len(observations),
    }, ensure_ascii=True))


_TREE_SCRIPT = r"""
({treeId}) => {
  const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const tree = window.jQuery?.fn?.zTree?.getZTreeObj(treeId);
  if (!tree) return null;
  return tree.transformToArray(tree.getNodes()).map((node) => {
    const parent = typeof node.getParentNode === "function" ? node.getParentNode() : null;
    const path = typeof node.getPath === "function" ? node.getPath() : [];
    return {
      id: clean(node.id),
      parent_id: clean(parent?.id),
      name: clean(node.name),
      path: path.map((item) => clean(item.name)).filter(Boolean),
      has_children: Boolean(node.isParent),
    };
  });
}
"""

_LIST_SCRIPT = r"""
() => {
  const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const headers = Array.from(document.querySelectorAll("#hTablememberlist th"))
    .map((cell) => clean(cell.innerText || cell.textContent));
  const rows = Array.from(document.querySelectorAll("#bTablememberlist tr")).map((row) => {
    const cells = Array.from(row.querySelectorAll("td"))
      .map((cell) => clean(cell.innerText || cell.textContent));
    const idInput = row.querySelector('input[name="id"]');
    const cardLink = row.querySelector('a[href*="showV3XMemberCard"], a[onclick*="showV3XMemberCard"]');
    const cardSource = `${cardLink?.getAttribute("href") || ""} ${cardLink?.getAttribute("onclick") || ""}`;
    const cardMatch = cardSource.match(/showV3XMemberCard\(['\"]?([^'\",)]+)/i);
    return {
      person_id: clean(idInput?.value || cardMatch?.[1]),
      cells,
    };
  }).filter((row) => row.cells.some(Boolean));
  const bodyText = clean(document.body?.innerText || "");
  const totalMatch = bodyText.match(/(?:共|总数)\s*(\d+)\s*(?:条|人)?/);
  const lastLink = Array.from(document.querySelectorAll('a[href*="last("]'))
    .map((link) => link.getAttribute("href") || "")
    .find(Boolean) || "";
  const pageMatch = lastLink.match(/last\([^,]+,\s*['\"]?(\d+)/);
  return {
    headers,
    rows,
    total: totalMatch ? Number(totalMatch[1]) : null,
    total_pages: pageMatch ? Number(pageMatch[1]) : (rows.length ? 1 : 0),
  };
}
"""

_GROUP_CONTRACTS = {
    "private": {
        "home_type": 2,
        "tree_method": "treeOwnTeam",
        "tree_query": {"addressbookType": 2},
        "list_method": "listOwnTeamMembers",
        "list_query": {"addressbookType": 2},
        "root_ids": {"-2"},
        "row_kind": "private",
    },
    "personal": {
        "home_type": 4,
        "tree_method": "treeOwnTeam",
        "tree_query": {},
        "list_method": "listOwnTeamMembers",
        "list_query": {"addressbookType": 4},
        "root_ids": {"-2"},
        "row_kind": "member",
    },
    "system": {
        "home_type": 3,
        "tree_method": "treeSysTeam",
        "tree_query": {"addressbookType": 1, "teamType": 2},
        "list_method": "listSysTeamMembers",
        "list_query": {"addressbookType": 3},
        "root_ids": {"-1"},
        "row_kind": "member",
    },
    "project": {
        "home_type": 6,
        "tree_method": "treeSysTeam",
        "tree_query": {"addressbookType": 1, "teamType": 3},
        "list_method": "listSysTeamMembers",
        "list_query": {"addressbookType": 3},
        "root_ids": {"-1"},
        "row_kind": "member",
    },
}


def invoke_addressbook_capability(
    capability_name: str,
    worker,
    *,
    base_url: str,
    arguments: dict,
) -> dict:
    if capability_name == ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY:
        return organization_tree(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY:
        return department_members(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_PERSON_SEARCH_CAPABILITY:
        return person_search(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_PERSON_GET_CAPABILITY:
        return person_get(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_GROUP_LIST_CAPABILITY:
        return group_list(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY:
        return group_members(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY:
        return private_contact_search(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_PRIVATE_CONTACT_GET_CAPABILITY:
        return private_contact_get(worker, base_url=base_url, arguments=arguments)
    if capability_name == ADDRESSBOOK_EXPORT_CAPABILITY:
        return export_addressbook(worker, base_url=base_url, arguments=arguments)
    raise KeyError(f"unsupported Seeyon address-book capability: {capability_name}")


def organization_tree(worker, *, base_url: str, arguments: dict) -> dict:
    keyword = _optional_text(arguments.get("keyword"), "keyword", maximum=200)
    limit = _integer(arguments.get("limit"), "limit", default=200, maximum=500)
    started = time.monotonic()
    try:
        page, account_id = _open_home(worker, base_url=base_url, addressbook_type=1)
    except AdapterLoginRequired:
        raise
    except Exception:
        raise SeeyonAddressbookContractMismatch(json.dumps({
            "code": "OA_ORG_HOME_UNAVAILABLE", "stage": "home",
            "elapsedMs": round((time.monotonic() - started) * 1000),
        })) from None
    home_ms = round((time.monotonic() - started) * 1000)
    try:
        tree_frame = _wait_for_frame(page, "method=treeDept")
    except AdapterLoginRequired:
        raise
    except Exception:
        _assert_not_login(str(getattr(page, "url", "")))
        raise SeeyonAddressbookContractMismatch(json.dumps({
            "code": "OA_ORG_FRAME_NOT_READY", "stage": "tree_frame", "frameReady": False,
            "homeElapsedMs": home_ms, "elapsedMs": round((time.monotonic() - started) * 1000),
        })) from None
    nodes, diagnostics = _organization_nodes(page, tree_frame)
    account_node = next(
        (node for node in nodes if str(node.get("id") or "") == account_id),
        nodes[0],
    )
    departments = []
    for node in nodes:
        department_id = str(node.get("id") or "").strip()
        name = str(node.get("name") or "").strip()
        if not department_id or not name or department_id == account_id:
            continue
        item = {
            "department_id": department_id,
            "parent_department_id": str(node.get("parent_id") or "").strip() or None,
            "name": name,
            "path": [str(value) for value in node.get("path") or [] if value],
            "has_children": bool(node.get("has_children")),
        }
        # Some OA zTree versions expose names and parents but no node.getPath().
        search_text = " ".join([name, *item["path"]]).casefold()
        if keyword and keyword.casefold() not in search_text:
            continue
        departments.append(item)
    matched_count = len(departments)
    departments = departments[:limit]
    return {
        "schema_version": "agentbridge.oa_addressbook_organization_tree.v1",
        "diagnostics": {"homeElapsedMs": home_ms,
                        "totalElapsedMs": round((time.monotonic() - started) * 1000),
                        "readinessObservations": diagnostics},
        "organization": {
            "account_id": account_id,
            "name": str(account_node.get("name") or "").strip(),
        },
        "matched_count": matched_count,
        "count": len(departments),
        "items": departments,
        "transport": "central_browser_session",
    }


def department_members(worker, *, base_url: str, arguments: dict) -> dict:
    department_id = _identifier(arguments.get("department_id"), "department_id")
    keyword = _optional_text(arguments.get("keyword"), "keyword", maximum=200)
    search_type = _search_type(arguments.get("search_type"))
    include_descendants = _boolean(
        arguments.get("include_descendants"), "include_descendants", default=False
    )
    limit = _integer(arguments.get("limit"), "limit", default=50, maximum=500)
    _page, account_id = _open_home(worker, base_url=base_url, addressbook_type=1)
    query = {
        "method": "listDeptMembers",
        "addressbookType": 1,
        "showType": "list",
        "pId": department_id,
        "accountId": account_id,
        "click": "dept",
        "deptId": department_id,
        "isDepartment": 1,
        "sonDepartmentMembers": "true" if include_descendants else "false",
        "pageSize": limit,
    }
    _append_search(query, keyword, search_type)
    raw = _read_list(worker, base_url=base_url, query=query)
    items = [_member_row(row, kind="organization") for row in raw["rows"]]
    return _list_result(
        "agentbridge.oa_addressbook_department_members.v1",
        items,
        raw,
        limit=limit,
        extra={
            "department_id": department_id,
            "include_descendants": include_descendants,
            "search_type": search_type,
            "keyword": keyword or None,
        },
    )


def person_search(worker, *, base_url: str, arguments: dict) -> dict:
    query_text = _required_text(arguments.get("query"), "query", maximum=200)
    search_type = _search_type(arguments.get("search_type"))
    limit = _integer(arguments.get("limit"), "limit", default=20, maximum=500)
    _page, account_id = _open_home(worker, base_url=base_url, addressbook_type=1)
    query = {
        "method": "initList",
        "addressbookType": 1,
        "showType": "list",
        "accountId": account_id,
        "pageSize": limit,
    }
    _append_search(query, query_text, search_type)
    raw = _read_list(worker, base_url=base_url, query=query)
    items = [_member_row(row, kind="organization") for row in raw["rows"]]
    return _list_result(
        "agentbridge.oa_addressbook_person_search.v1",
        items,
        raw,
        limit=limit,
        extra={"query": query_text, "search_type": search_type},
    )


def person_get(worker, *, base_url: str, arguments: dict) -> dict:
    reference = _decode_reference(arguments.get("person_ref"), expected_kind="person")
    result = person_search(
        worker,
        base_url=base_url,
        arguments={"query": reference["name"], "search_type": "name", "limit": 100},
    )
    item = next(
        (
            candidate
            for candidate in result["items"]
            if candidate.get("person_id") == reference["id"]
        ),
        None,
    )
    if item is None:
        raise SeeyonAddressbookContractMismatch(
            "The selected OA person is no longer visible in the current user's address book."
        )
    return {
        "schema_version": "agentbridge.oa_addressbook_person_detail.v1",
        "item": item,
        "detail_visibility": "directory_row",
        "masked_values_preserved": True,
        "transport": "central_browser_session",
    }


def group_list(worker, *, base_url: str, arguments: dict) -> dict:
    requested_type = arguments.get("group_type")
    group_types = (
        [_group_type(requested_type)] if requested_type is not None else list(ADDRESSBOOK_GROUP_TYPES)
    )
    keyword = _optional_text(arguments.get("keyword"), "keyword", maximum=200)
    limit = _integer(arguments.get("limit"), "limit", default=100, maximum=500)
    items = []
    for group_type in group_types:
        contract = _GROUP_CONTRACTS[group_type]
        page, account_id = _open_home(
            worker,
            base_url=base_url,
            addressbook_type=contract["home_type"],
        )
        tree_frame = _wait_for_frame(page, f"method={contract['tree_method']}")
        nodes = tree_frame.evaluate(_TREE_SCRIPT, {"treeId": "accountTree"})
        if not isinstance(nodes, list):
            raise SeeyonAddressbookContractMismatch(
                f"The OA {group_type} group tree is unavailable."
            )
        for node in nodes:
            group_id = str(node.get("id") or "").strip()
            name = str(node.get("name") or "").strip()
            if not group_id or not name or group_id in contract["root_ids"]:
                continue
            item = {
                "group_type": group_type,
                "group_id": group_id,
                "name": name,
                "parent_group_id": str(node.get("parent_id") or "").strip() or None,
                "path": [str(value) for value in node.get("path") or [] if value],
                "has_children": bool(node.get("has_children")),
                "account_id": account_id,
            }
            if keyword and keyword.casefold() not in " ".join(item["path"]).casefold():
                continue
            items.append(item)
    matched_count = len(items)
    items = items[:limit]
    return {
        "schema_version": "agentbridge.oa_addressbook_group_list.v1",
        "group_types": group_types,
        "matched_count": matched_count,
        "count": len(items),
        "items": items,
        "transport": "central_browser_session",
    }


def group_members(worker, *, base_url: str, arguments: dict) -> dict:
    group_type = _group_type(arguments.get("group_type"))
    group_id = _identifier(arguments.get("group_id"), "group_id")
    keyword = _optional_text(arguments.get("keyword"), "keyword", maximum=200)
    search_type = _search_type(arguments.get("search_type"))
    limit = _integer(arguments.get("limit"), "limit", default=50, maximum=500)
    contract = _GROUP_CONTRACTS[group_type]
    _page, account_id = _open_home(
        worker,
        base_url=base_url,
        addressbook_type=contract["home_type"],
    )
    query = {
        "method": contract["list_method"],
        **contract["list_query"],
        "showType": "list",
        "accountId": account_id,
        "tId": group_id,
        "pageSize": limit,
    }
    _append_search(query, keyword, search_type)
    raw = _read_list(worker, base_url=base_url, query=query)
    items = [_member_row(row, kind=contract["row_kind"]) for row in raw["rows"]]
    return _list_result(
        "agentbridge.oa_addressbook_group_members.v1",
        items,
        raw,
        limit=limit,
        extra={
            "group_type": group_type,
            "group_id": group_id,
            "search_type": search_type,
            "keyword": keyword or None,
        },
    )


def private_contact_search(worker, *, base_url: str, arguments: dict) -> dict:
    query_text = _optional_text(arguments.get("query"), "query", maximum=200)
    search_type = _search_type(arguments.get("search_type"))
    group_id = _optional_identifier(arguments.get("group_id"), "group_id")
    limit = _integer(arguments.get("limit"), "limit", default=50, maximum=500)
    _page, account_id = _open_home(worker, base_url=base_url, addressbook_type=2)
    query = {
        "method": "listOwnTeamMembers" if group_id else "initList",
        "addressbookType": 2,
        "showType": "list",
        "accountId": account_id,
        "pageSize": limit,
    }
    if group_id:
        query["tId"] = group_id
    _append_search(query, query_text, search_type)
    raw = _read_list(worker, base_url=base_url, query=query)
    items = [_member_row(row, kind="private") for row in raw["rows"]]
    return _list_result(
        "agentbridge.oa_addressbook_private_contact_search.v1",
        items,
        raw,
        limit=limit,
        extra={
            "query": query_text or None,
            "search_type": search_type,
            "group_id": group_id,
        },
    )


def private_contact_get(worker, *, base_url: str, arguments: dict) -> dict:
    reference = _decode_reference(arguments.get("contact_ref"), expected_kind="contact")
    result = private_contact_search(
        worker,
        base_url=base_url,
        arguments={"query": reference["name"], "search_type": "name", "limit": 100},
    )
    item = next(
        (
            candidate
            for candidate in result["items"]
            if candidate.get("contact_id") == reference["id"]
        ),
        None,
    )
    if item is None:
        raise SeeyonAddressbookContractMismatch(
            "The selected private contact is no longer visible in the current user's address book."
        )
    return {
        "schema_version": "agentbridge.oa_addressbook_private_contact_detail.v1",
        "item": item,
        "detail_visibility": "directory_row",
        "masked_values_preserved": True,
        "transport": "central_browser_session",
    }


def export_addressbook(worker, *, base_url: str, arguments: dict) -> dict:
    source = str(arguments.get("source") or "").strip()
    if source not in ADDRESSBOOK_EXPORT_SOURCES:
        raise ValueError(
            "source must be one of: " + ", ".join(ADDRESSBOOK_EXPORT_SOURCES)
        )
    limit = _integer(
        arguments.get("limit"),
        "limit",
        default=ADDRESSBOOK_EXPORT_MAX_ROWS,
        maximum=ADDRESSBOOK_EXPORT_MAX_ROWS,
    )
    if source == "person_search":
        result = person_search(
            worker,
            base_url=base_url,
            arguments={
                "query": _required_text(arguments.get("query"), "query", maximum=200),
                "search_type": _search_type(arguments.get("search_type")),
                "limit": limit,
            },
        )
        title = "OA组织通讯录查询结果"
    elif source == "department_members":
        result = department_members(
            worker,
            base_url=base_url,
            arguments={
                "department_id": _identifier(
                    arguments.get("department_id"), "department_id"
                ),
                "keyword": arguments.get("query"),
                "search_type": _search_type(arguments.get("search_type")),
                "include_descendants": _boolean(
                    arguments.get("include_descendants"),
                    "include_descendants",
                    default=False,
                ),
                "limit": limit,
            },
        )
        title = "OA部门成员"
    elif source == "group_members":
        result = group_members(
            worker,
            base_url=base_url,
            arguments={
                "group_type": _group_type(arguments.get("group_type")),
                "group_id": _identifier(arguments.get("group_id"), "group_id"),
                "keyword": arguments.get("query"),
                "search_type": _search_type(arguments.get("search_type")),
                "limit": limit,
            },
        )
        title = "OA通讯录组成员"
    else:
        result = private_contact_search(
            worker,
            base_url=base_url,
            arguments={
                "query": arguments.get("query"),
                "search_type": _search_type(arguments.get("search_type")),
                "group_id": arguments.get("group_id"),
                "limit": limit,
            },
        )
        title = "OA私人通讯录"

    private = source == "private_contacts"
    columns = (
        [
            {"key": "name", "label": "姓名"},
            {"key": "company", "label": "单位名称"},
            {"key": "job_level", "label": "职务级别"},
            {"key": "office_phone", "label": "办公电话"},
            {"key": "mobile_phone", "label": "手机号码"},
        ]
        if private
        else [
            {"key": "name", "label": "姓名"},
            {"key": "person_code", "label": "人员编号"},
            {"key": "department", "label": "部门"},
            {"key": "position", "label": "岗位"},
            {"key": "office_phone", "label": "办公电话"},
            {"key": "mobile_phone", "label": "手机号码"},
        ]
    )
    rows = [
        {column["key"]: item.get(column["key"]) for column in columns}
        for item in result.get("items") or []
    ]
    return {
        "schemaVersion": "agentbridge.oa-addressbook-export.v1",
        "reportType": source,
        "reportTitle": title,
        "filenameStem": title,
        "columns": columns,
        "rows": rows,
        "metadata": {
            "source": source,
            "rowCount": len(rows),
            "sourceTotal": result.get("source_total"),
            "truncated": bool(result.get("truncated")),
            "maskedValuesPreserved": True,
        },
    }


def _open_home(worker, *, base_url: str, addressbook_type: int):
    page = worker.goto(
        urljoin(
            base_url,
            "addressbook.do?"
            + urlencode({"method": "home", "addressbookType": addressbook_type}),
        ),
        timeout_seconds=60,
    )
    _assert_not_login(worker.page_url)
    home_frame = _wait_for_frame(page, f"method=home&addressbookType={addressbook_type}")
    account_id = str(
        home_frame.evaluate(
            "() => document.getElementById('accountId')?.value || ''"
        )
        or ""
    ).strip()
    if not re.fullmatch(r"-?\d{1,32}", account_id):
        raise SeeyonAddressbookContractMismatch(
            "The OA address book did not expose a valid organization account."
        )
    return page, account_id


def _wait_for_frame(page, url_fragment: str, *, timeout_seconds: float = 12):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _assert_not_login(str(getattr(page, "url", "")))
        for frame in page.frames:
            if url_fragment in str(frame.url or ""):
                return frame
        page.wait_for_timeout(100)
    raise SeeyonAddressbookContractMismatch(
        f"The OA address book did not expose the expected frame ({url_fragment})."
    )


def _read_list(worker, *, base_url: str, query: dict) -> dict:
    page = worker.goto(
        urljoin(base_url, "addressbook.do?" + urlencode(query)),
        timeout_seconds=60,
    )
    _assert_not_login(worker.page_url)
    try:
        page.wait_for_selector("#hTablememberlist", state="attached", timeout=12000)
        raw = page.evaluate(_LIST_SCRIPT)
    except Exception as exc:
        _assert_not_login(worker.page_url)
        raise SeeyonAddressbookContractMismatch(
            "The OA address-book member list did not expose its expected table."
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        raise SeeyonAddressbookContractMismatch(
            "The OA address-book member list returned an invalid result."
        )
    headers = [str(value or "").strip() for value in raw.get("headers") or []]
    normalized_rows = []
    for row in raw["rows"]:
        if not isinstance(row, dict):
            continue
        cells = [str(value or "").strip() for value in row.get("cells") or []]
        if len(cells) != len(headers):
            continue
        normalized_rows.append(
            {
                "person_id": str(row.get("person_id") or "").strip(),
                "values": dict(zip(headers, cells)),
            }
        )
    return {
        "rows": normalized_rows,
        "total": raw.get("total"),
        "total_pages": raw.get("total_pages"),
    }


def _member_row(row: dict, *, kind: str) -> dict:
    values = row.get("values") or {}
    person_id = _identifier(row.get("person_id"), "person_id")
    name = str(values.get("姓名") or "").strip()
    if not name:
        raise SeeyonAddressbookContractMismatch(
            "An OA address-book row did not contain a visible name."
        )
    if kind == "private":
        return {
            "contact_id": person_id,
            "contact_ref": _encode_reference("contact", person_id, name),
            "name": name,
            "company": str(values.get("单位名称") or "").strip(),
            "job_level": str(values.get("职务级别") or "").strip(),
            "office_phone": str(values.get("办公电话") or "").strip(),
            "mobile_phone": str(values.get("手机号码") or "").strip(),
        }
    return {
        "person_id": person_id,
        "person_ref": _encode_reference("person", person_id, name),
        "name": name,
        "person_code": str(values.get("人员编号") or "").strip(),
        "department": str(values.get("部门") or "").strip(),
        "position": str(values.get("岗位") or "").strip(),
        "office_phone": str(values.get("办公电话") or "").strip(),
        "mobile_phone": str(values.get("手机号码") or "").strip(),
    }


def _list_result(
    schema_version: str,
    items: list[dict],
    raw: dict,
    *,
    limit: int,
    extra: dict,
) -> dict:
    source_total = raw.get("total")
    if not isinstance(source_total, int):
        source_total = len(items)
    return {
        "schema_version": schema_version,
        **extra,
        "source_total": source_total,
        "count": len(items),
        "limit": limit,
        "truncated": source_total > len(items),
        "items": items,
        "masked_values_preserved": True,
        "transport": "central_browser_session",
    }


def _append_search(query: dict, keyword: str, search_type: str) -> None:
    if keyword:
        query["searchContent"] = keyword
        query["searchType"] = search_type


def _encode_reference(kind: str, identifier: str, name: str) -> str:
    payload = json.dumps(
        {"kind": kind, "id": identifier, "name": name},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_reference(value, *, expected_kind: str) -> dict:
    reference = _required_text(value, f"{expected_kind}_ref", maximum=1024)
    try:
        payload = base64.urlsafe_b64decode(reference + "=" * (-len(reference) % 4))
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{expected_kind}_ref is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("kind") != expected_kind:
        raise ValueError(f"{expected_kind}_ref is invalid")
    return {
        "id": _identifier(decoded.get("id"), f"{expected_kind}_ref.id"),
        "name": _required_text(
            decoded.get("name"), f"{expected_kind}_ref.name", maximum=200
        ),
    }


def _assert_not_login(url: str) -> None:
    parsed = urlparse(str(url or ""))
    value = f"{parsed.path}?{parsed.query}".lower()
    if "login" in value or "method=logout" in value:
        raise AdapterLoginRequired("The central OA session expired while reading the address book.")


def _identifier(value, name: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"-?\d{1,32}", text):
        raise ValueError(f"{name} is invalid")
    return text


def _optional_identifier(value, name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _identifier(value, name)


def _required_text(value, name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return text


def _optional_text(value, name: str, *, maximum: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters")
    return text


def _integer(value, name: str, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 1 or parsed > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return parsed


def _boolean(value, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _search_type(value) -> str:
    selected = str(value or "name").strip()
    if selected not in {"name", "all"}:
        raise ValueError("search_type must be one of: name, all")
    return selected


def _group_type(value) -> str:
    selected = str(value or "").strip()
    if selected not in ADDRESSBOOK_GROUP_TYPES:
        raise ValueError(
            "group_type must be one of: " + ", ".join(ADDRESSBOOK_GROUP_TYPES)
        )
    return selected
