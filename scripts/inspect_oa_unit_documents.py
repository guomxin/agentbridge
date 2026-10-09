from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.core.config import ConfigStore
from agentbridge.core.session_secrets import SessionStateStore
from agentbridge.core.sessions import SessionRegistry


_BLOCKED_WRITE_MARKERS = (
    "delete",
    "remove",
    "save",
    "submit",
    "update",
    "upload",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the OA Unit Documents entry without downloading files."
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--user-subject", required=True)
    parser.add_argument("--keyword", default="\u5355\u4f4d\u6587\u6863")
    parser.add_argument("--open-via", default="\u5168\u90e8\u5e94\u7528")
    parser.add_argument(
        "--search-placeholder",
        default="\u8bf7\u8f93\u5165\u5173\u952e\u5b57\u641c\u7d22",
    )
    parser.add_argument("--nested-keyword", default="\u5355\u4f4d\u6587\u6863")
    parser.add_argument(
        "--folder-keyword",
        default="\u77e5\u8bc6\u4ea7\u6743\u6587\u6863",
    )
    parser.add_argument("--subfolder-keyword", default="")
    parser.add_argument("--category-keyword", default="")
    parser.add_argument("--probe-search-name", default="")
    parser.add_argument("--settle-ms", type=int, default=2500)
    args = parser.parse_args()

    home = Path(args.home).resolve()
    profile = ConfigStore(home).load_system("oa")
    registry = SessionRegistry(home / "agentbridge.db", home / "profiles")
    session = registry.find(user_subject=args.user_subject, system_id="oa")
    if session is None or session["state"] != "active":
        raise RuntimeError("one active OA session is required")
    state = SessionStateStore(home / "session-secrets").load(session["session_id"])
    if state is None:
        raise RuntimeError("the active OA session has no encrypted browser state")

    adapter = SeeyonCentralAdapter(base_url=profile.base_url)
    network_events: list[dict] = []
    blocked_requests: list[dict] = []
    with CentralBrowserWorker(
        profile_path=session["profile_path"],
        allowed_origins={adapter.origin},
        headless=True,
    ) as worker:
        worker.restore_session_state(state)
        page = worker.goto(profile.base_url, timeout_seconds=60)
        page.context.on(
            "request",
            lambda request: network_events.append(_request_summary(request)),
        )
        page.context.route(
            "**/*",
            lambda route, request: _guard_request(
                route,
                request,
                blocked_requests=blocked_requests,
            ),
        )
        if args.settle_ms > 0:
            page.wait_for_timeout(args.settle_ms)

        before = _inspect_pages(page.context.pages, keyword=args.keyword)
        opened_via = None
        searched = None
        via = None
        if args.open_via:
            opened_via = _click_exact_visible_text(page.context.pages, args.open_via)
            if opened_via:
                page.wait_for_timeout(args.settle_ms)
                searched = _fill_first_visible(
                    page.context.pages,
                    placeholder=args.search_placeholder,
                    value=args.keyword,
                )
                if searched:
                    page.wait_for_timeout(args.settle_ms)
                via = _inspect_pages(page.context.pages, keyword=args.keyword)
        clicked = _click_exact_visible_text(page.context.pages, args.keyword)
        if clicked:
            page.wait_for_timeout(args.settle_ms)
        target = _inspect_pages(page.context.pages, keyword=args.nested_keyword)
        nested_clicked = None
        if args.nested_keyword:
            nested_clicked = _click_exact_visible_text(
                page.context.pages,
                args.nested_keyword,
            )
            if nested_clicked:
                page.wait_for_timeout(args.settle_ms)
        unit_documents = _inspect_pages(
            page.context.pages,
            keyword=args.folder_keyword or args.nested_keyword,
        )
        folder_clicked = None
        if args.folder_keyword:
            folder_clicked = _click_exact_visible_text(
                page.context.pages,
                args.folder_keyword,
            )
            if folder_clicked:
                page.wait_for_timeout(args.settle_ms)
        folder = _inspect_pages(
            page.context.pages,
            keyword=args.subfolder_keyword or args.folder_keyword,
        )
        subfolder_clicked = None
        if args.subfolder_keyword:
            subfolder_clicked = _click_exact_visible_text(
                page.context.pages,
                args.subfolder_keyword,
            )
            if subfolder_clicked:
                page.wait_for_timeout(args.settle_ms)
        subfolder = _inspect_pages(
            page.context.pages,
            keyword=args.category_keyword or args.subfolder_keyword,
        )
        category_clicked = None
        if args.category_keyword:
            category_clicked = _click_exact_visible_text(
                page.context.pages,
                args.category_keyword,
            )
            if category_clicked:
                page.wait_for_timeout(args.settle_ms)
        searched_documents = None
        if args.probe_search_name:
            searched_documents = _search_current_document_folder(
                page.context.pages,
                value=args.probe_search_name,
            )
            if searched_documents:
                page.wait_for_timeout(args.settle_ms)
        after = _inspect_pages(
            page.context.pages,
            keyword=args.folder_keyword or args.nested_keyword,
        )
        document_result_shape = _document_result_shape(page.context.pages)

    result = {
        "schema_version": "agentbridge.oa_unit_documents_probe.v1",
        "user_subject": args.user_subject,
        "keyword": args.keyword,
        "opened_via": opened_via,
        "searched": searched,
        "via": via,
        "clicked": clicked,
        "target": target,
        "nested_clicked": nested_clicked,
        "unit_documents": unit_documents,
        "folder_clicked": folder_clicked,
        "folder": folder,
        "subfolder_clicked": subfolder_clicked,
        "subfolder": subfolder,
        "category_clicked": category_clicked,
        "searched_documents": searched_documents,
        "before": before,
        "after": after,
        "document_result_shape": document_result_shape,
        "network": _deduplicate(network_events),
        "blocked_requests": _deduplicate(blocked_requests),
        "safety": {
            "downloads_started": 0,
            "write_controls_clicked": 0,
            "cookies_included": False,
            "field_values_included": False,
            "raw_html_included": False,
            "response_bodies_included": False,
        },
    }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _guard_request(route, request, *, blocked_requests: list[dict]) -> None:
    method = str(request.method or "").upper()
    parsed = urlparse(request.url)
    lowered = f"{parsed.path}?{parsed.query}".lower()
    blocked = method in {"DELETE", "PATCH", "PUT"} or (
        method == "POST" and any(marker in lowered for marker in _BLOCKED_WRITE_MARKERS)
    )
    if blocked:
        blocked_requests.append(_request_summary(request))
        route.abort()
        return
    route.continue_()


def _request_summary(request) -> dict:
    parsed = urlparse(request.url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    operation_keys = ("managerName", "method", "CL", "M")
    return {
        "method": str(request.method or "").upper(),
        "origin": _safe_origin(parsed),
        "path": parsed.path,
        "query_keys": sorted(query),
        "operation": {
            key: query[key][-1]
            for key in operation_keys
            if key in query and query[key]
        },
        "post_data_keys": _post_data_keys(request.post_data),
        "resource_type": str(request.resource_type or ""),
    }


def _post_data_keys(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return sorted(parse_qs(value, keep_blank_values=True))
    return sorted(parsed) if isinstance(parsed, dict) else []


def _inspect_pages(pages, *, keyword: str) -> list[dict]:
    return [
        {
            "url": _safe_url(page.url),
            "title": page.title(),
            "frames": [_inspect_frame(frame, keyword=keyword) for frame in page.frames],
        }
        for page in pages
    ]


def _inspect_frame(frame, *, keyword: str) -> dict:
    structure = frame.evaluate(
        """
        (keyword) => {
          const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && box.width > 0 && box.height > 0;
          };
          const actionPath = (value) => {
            try { return new URL(value, location.href).pathname; }
            catch (_) { return ''; }
          };
          const candidates = Array.from(
            document.querySelectorAll('a,button,[role="button"],[onclick]')
          )
            .filter((element) => clean(element.innerText || element.textContent) === keyword)
            .map((element) => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || '',
              text: keyword,
              visible: visible(element),
              href_path: actionPath(element.getAttribute('href') || ''),
              onclick_prefix: clean(element.getAttribute('onclick')).slice(0, 160),
            }));
          const exactTextNodes = Array.from(document.querySelectorAll('body *'))
            .filter(visible)
            .filter((element) => clean(element.innerText || element.textContent) === keyword)
            .map((element) => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || '',
              class_prefix: clean(element.className).slice(0, 120),
              role: element.getAttribute('role') || '',
              clickable: element.matches('a,button,[role="button"],[onclick]'),
            }))
            .slice(0, 40);
          const keywordTexts = Array.from(document.querySelectorAll('body *'))
            .filter(visible)
            .map((element) => clean(element.innerText || element.textContent))
            .filter((value) => value.includes(keyword) && value.length <= 120)
            .filter((value, index, values) => values.indexOf(value) === index)
            .slice(0, 40);
          const shortClickables = Array.from(
            document.querySelectorAll('a,button,[role="button"],[onclick],[tabindex]')
          )
            .filter(visible)
            .map((element) => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || '',
              text: clean(element.innerText || element.textContent).slice(0, 80),
              href_path: actionPath(element.getAttribute('href') || ''),
            }))
            .filter((item) => item.text && item.text.length <= 40)
            .slice(0, 240);
          const tableRows = Array.from(document.querySelectorAll('tr'))
            .filter(visible)
            .map((row) => ({
              id: row.id || '',
              class_prefix: clean(row.className).slice(0, 120),
              text: clean(row.innerText || row.textContent).slice(0, 500),
              cells: Array.from(row.querySelectorAll(':scope > th,:scope > td')).map((cell) => ({
                class_prefix: clean(cell.className).slice(0, 120),
                text: clean(cell.innerText || cell.textContent).slice(0, 240),
              })).slice(0, 20),
            }))
            .filter((row) => row.text)
            .slice(0, 120);
          const actionControls = Array.from(
            document.querySelectorAll(
              'a,button,[role="button"],[onclick],i,[class*="search"],[id*="search"]'
            )
          )
            .filter(visible)
            .map((element) => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || '',
              class_prefix: clean(element.className).slice(0, 160),
              text: clean(element.innerText || element.textContent).slice(0, 100),
              onclick_prefix: clean(element.getAttribute('onclick')).slice(0, 160),
              role: element.getAttribute('role') || '',
            }))
            .slice(0, 300);
          const labels = Array.from(
            document.querySelectorAll('h1,h2,h3,label,button,[role="button"],input[placeholder]')
          )
            .filter(visible)
            .map((element) => clean(
              element.innerText || element.textContent || element.getAttribute('placeholder')
            ).slice(0, 120))
            .filter(Boolean)
            .filter((value, index, values) => values.indexOf(value) === index)
            .slice(0, 120);
          return {
            title: document.title,
            body_text_length: clean(document.body && document.body.innerText).length,
            body_has_keyword: clean(document.body && document.body.innerText).includes(keyword),
            candidates,
            exact_text_nodes: exactTextNodes,
            keyword_texts: keywordTexts,
            short_clickables: shortClickables,
            action_controls: actionControls,
            table_rows: tableRows,
            labels,
            forms: Array.from(document.forms).map((form) => ({
              id: form.id || '',
              name: form.getAttribute('name') || '',
              method: String(form.method || '').toUpperCase(),
              action_path: actionPath(form.action || ''),
            })),
            controls: Array.from(document.querySelectorAll('input,select,textarea'))
              .filter(visible)
              .map((element) => ({
                tag: element.tagName.toLowerCase(),
                id: element.id || '',
                name: element.getAttribute('name') || '',
                type: element.getAttribute('type') || '',
                placeholder: clean(element.getAttribute('placeholder')).slice(0, 120),
                class_prefix: clean(element.className).slice(0, 120),
                aria_label: clean(element.getAttribute('aria-label')).slice(0, 120),
                parent: element.parentElement ? {
                  tag: element.parentElement.tagName.toLowerCase(),
                  id: element.parentElement.id || '',
                  class_prefix: clean(element.parentElement.className).slice(0, 120),
                  text: clean(element.parentElement.innerText).slice(0, 160),
                  children: Array.from(element.parentElement.children).map((child) => ({
                    tag: child.tagName.toLowerCase(),
                    id: child.id || '',
                    class_prefix: clean(child.className).slice(0, 120),
                    text: clean(child.innerText || child.textContent).slice(0, 80),
                    role: child.getAttribute('role') || '',
                  })).slice(0, 20),
                } : null,
              }))
              .slice(0, 120),
          };
        }
        """,
        keyword,
    )
    return {
        **structure,
        "url": _safe_url(frame.url),
    }


def _search_current_document_folder(pages, *, value: str) -> dict | None:
    for page_index, page in enumerate(pages):
        for frame_index, frame in enumerate(page.frames):
            field = frame.locator("#frName")
            button = frame.locator("a.syIcon.sy-search.seary-bar-btn")
            if not field.count() or not button.count():
                continue
            if not field.first.is_visible() or not button.first.is_visible():
                continue
            field.first.fill(value, timeout=5000)
            button.first.click(timeout=5000)
            return {
                "page_index": page_index,
                "frame_index": frame_index,
                "value_length": len(value),
                "field_id": "frName",
                "button_class": "syIcon sy-search seary-bar-btn",
            }
    return None


def _document_result_shape(pages) -> dict | None:
    for page_index, page in enumerate(pages):
        for frame_index, frame in enumerate(page.frames):
            result = frame.evaluate(
                """
                () => {
                  const rows = Object.values(window.nowPageDr || {});
                  const row = rows.find(
                    (item) => item && item.docResource && !item.docResource.isFolder
                  );
                  if (!row) return null;
                  return {
                    page_keys: Object.keys(row).sort(),
                    resource_keys: Object.keys(row.docResource || {}).sort(),
                    booleans: {
                      download_acl: Boolean(row.downloadAcl),
                      is_folder: Boolean(row.docResource && row.docResource.isFolder),
                      is_upload_file: row.isUploadFile === true || row.isUploadFile === 'true',
                    },
                    field_types: {
                      id: typeof row.id,
                      name: typeof row.docResource.frName,
                      size: typeof row.docResource.frSize,
                      source_id: typeof row.docResource.sourceId,
                      create_date: typeof row.createDate,
                      version: typeof row.vForDocDownload,
                      mime_type_id: typeof row.docResource.mimeTypeId,
                      secret_level: typeof row.docResource.secretLevel,
                    },
                  };
                }
                """
            )
            if result:
                return {
                    "page_index": page_index,
                    "frame_index": frame_index,
                    **result,
                }
    return None

def _fill_first_visible(pages, *, placeholder: str, value: str) -> dict | None:
    for page_index, page in enumerate(pages):
        for frame_index, frame in enumerate(page.frames):
            locator = frame.get_by_placeholder(placeholder, exact=True)
            for match_index in range(locator.count()):
                candidate = locator.nth(match_index)
                if not candidate.is_visible():
                    continue
                candidate.fill("", timeout=5000)
                candidate.press_sequentially(value, delay=80, timeout=10000)
                icon_clicked = candidate.evaluate(
                    """
                    (input) => {
                      const icon = input.parentElement
                        && input.parentElement.querySelector('i.vportal.vp-search-large');
                      if (!icon) return false;
                      icon.click();
                      return true;
                    }
                    """
                )
                trigger = "native_search_icon_click" if icon_clicked else "keyboard_input"
                return {
                    "page_index": page_index,
                    "frame_index": frame_index,
                    "match_index": match_index,
                    "placeholder": placeholder,
                    "trigger": trigger,
                }
    return None


def _click_exact_visible_text(pages, keyword: str) -> dict | None:
    selectors = (
        'a,button,[role="button"],[onclick]',
        '.menu-expand-font,.menu-list-li-title,.quick-search-menu-item,.menu_btn.text_overflow',
    )
    for page_index, page in enumerate(pages):
        for frame_index, frame in enumerate(page.frames):
            for selector_index, selector in enumerate(selectors):
                locator = frame.locator(selector)
                for match_index in range(locator.count()):
                    candidate = locator.nth(match_index)
                    text = " ".join((candidate.inner_text() or "").split())
                    if text != keyword or not candidate.is_visible():
                        continue
                    if selector_index == 0:
                        candidate.click(timeout=5000)
                    else:
                        candidate.evaluate("(element) => element.click()")
                    return {
                        "page_index": page_index,
                        "frame_index": frame_index,
                        "match_index": match_index,
                        "selector": selector,
                        "text": keyword,
                    }
    return None


def _safe_url(value: str) -> dict:
    parsed = urlparse(str(value or ""))
    return {
        "origin": _safe_origin(parsed),
        "path": parsed.path,
        "query_keys": sorted(parse_qs(parsed.query, keep_blank_values=True)),
    }


def _safe_origin(parsed) -> str:
    host = parsed.hostname or ""
    if not host:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _deduplicate(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=True, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
