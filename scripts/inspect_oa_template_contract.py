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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a Seeyon launch template without exposing values or clicking writes."
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--template-title", required=True)
    parser.add_argument("--user-subject")
    parser.add_argument("--settle-ms", type=int, default=1500)
    parser.add_argument("--expand-options", action="append", default=[])
    args = parser.parse_args()

    home = Path(args.home).resolve()
    profile = ConfigStore(home).load_system("oa")
    registry = SessionRegistry(home / "agentbridge.db", home / "profiles")
    sessions = registry.list_active(system_id="oa")
    if args.user_subject:
        sessions = [item for item in sessions if item["user_subject"] == args.user_subject]
    if len(sessions) != 1:
        raise RuntimeError("exactly one matching active OA session is required")

    session = sessions[0]
    state = SessionStateStore(home / "session-secrets").load(session["session_id"])
    if state is None:
        raise RuntimeError("the active OA session has no encrypted browser state")

    adapter = SeeyonCentralAdapter(base_url=profile.base_url)
    with CentralBrowserWorker(
        profile_path=session["profile_path"],
        allowed_origins={adapter.origin},
        headless=True,
    ) as worker:
        worker.restore_session_state(state)
        template = _resolve_template(adapter.list_templates(worker), args.template_title)
        page = worker.goto(template["href"], timeout_seconds=60)
        if args.settle_ms > 0:
            page.wait_for_timeout(args.settle_ms)
        _dismiss_read_only_notices(page)
        result = {
            "schema_version": "agentbridge.oa_template_contract_probe.v1",
            "template": {
                "title": template["title"],
                "template_id": template["template_id"],
                "form_app_id": template["form_app_id"],
            },
            "page": _inspect_main_page(page),
            "frames": [
                _inspect_frame(frame, expand_options=args.expand_options)
                for frame in page.frames
                if "/cap4/" in frame.url
            ],
            "safety": {
                "write_controls_clicked": 0,
                "field_values_included": False,
                "cookies_included": False,
                "raw_html_included": False,
            },
        }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _resolve_template(template_list: dict, title: str) -> dict:
    matches = [
        item
        for item in template_list.get("items") or []
        if isinstance(item, dict) and str(item.get("title") or "") == title
    ]
    if len(matches) != 1:
        raise RuntimeError(f"template title did not resolve uniquely: {title}")
    return matches[0]


def _inspect_main_page(page) -> dict:
    structure = page.evaluate(
        r"""
        () => {
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const visible = (element) => {
            const style = getComputedStyle(element);
            return style.display !== 'none' && style.visibility !== 'hidden';
          };
          return {
            title: document.title,
            handlers: Object.fromEntries(
              ['toolbarsendId_aClick', 'toolbarsaveDraft_aClick'].map((name) => [
                name,
                typeof window[name] === 'function'
                  ? String(window[name]).replace(/\s+/g, ' ').trim().slice(0, 3000)
                  : '',
              ])
            ),
            forms: Array.from(document.forms).map((form) => ({
              id: form.getAttribute('id') || '',
              name: form.getAttribute('name') || '',
              method: String(form.method || '').toUpperCase(),
              action_path: (() => {
                try { return new URL(form.action, location.href).pathname; }
                catch (_) { return ''; }
              })(),
            })),
            buttons: Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"]'))
              .filter((element) => element.id && visible(element))
              .map((element) => ({
                id: element.id,
                tag: element.tagName.toLowerCase(),
                text: clean(element.innerText || element.getAttribute('value')).slice(0, 120),
                onclick: clean(element.getAttribute('onclick')).slice(0, 240),
              })),
          };
        }
        """
    )
    parsed = urlparse(page.url)
    return {
        **structure,
        "url_path": parsed.path,
        "query_keys": sorted(parse_qs(parsed.query, keep_blank_values=True)),
    }


def _inspect_frame(frame, *, expand_options: list[str]) -> dict:
    structure = frame.evaluate(
        r"""
        () => {
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const controls = (wrapper) => Array.from(
            wrapper.querySelectorAll('input,textarea,select,button')
          ).map((element) => ({
            id: element.id || '',
            name: element.getAttribute('name') || '',
            tag: element.tagName.toLowerCase(),
            type: element.getAttribute('type') || '',
            readonly: Boolean(element.readOnly),
            disabled: Boolean(element.disabled),
          }));
          const safeTexts = (wrapper, selector) => Array.from(wrapper.querySelectorAll(selector))
            .map((element) => clean(element.innerText || element.textContent).slice(0, 120))
            .filter(Boolean);
          const fields = Array.from(document.querySelectorAll('[id^="field"][id$="_id"]'))
            .filter((element) => /^field\d+_id$/.test(element.id))
            .map((wrapper) => ({
              field: wrapper.id.slice(0, -3),
              class_names: Array.from(wrapper.classList).sort(),
              controls: controls(wrapper),
              label_candidates: safeTexts(
                wrapper,
                '[class*="label"],[class*="Label"],[class*="title"],[class*="Title"]'
              ),
              option_labels: safeTexts(
                wrapper,
                '.cap4-radio__item,.cap4-checkbox__item,option'
              ),
            }));
          return {
            title: document.title,
            fields,
          };
        }
        """
    )
    parsed = urlparse(frame.url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return {
        **structure,
        "expanded_options": {
            field: _read_expanded_options(frame, field) for field in expand_options
        },
        "url_path": parsed.path,
        "module_id": str(query.get("moduleId", [""])[0] or ""),
        "form_app_id": str(query.get("formAppId", [""])[0] or ""),
        "query_keys": sorted(query),
    }


def _dismiss_read_only_notices(page) -> None:
    buttons = page.locator('[id$="ok_msg_btn_first"]:visible')
    while buttons.count():
        buttons.first.click(timeout=3000)
        page.wait_for_timeout(100)
        buttons = page.locator('[id$="ok_msg_btn_first"]:visible')


def _read_expanded_options(frame, field: str) -> list[str]:
    if not field.startswith("field") or not field[5:].isdigit():
        raise ValueError(f"invalid CAP4 field identifier: {field}")
    wrapper = frame.locator(f"#{field}_id")
    if wrapper.count() != 1:
        raise RuntimeError(f"CAP4 option field was not found: {field}")
    wrapper.click(timeout=3000)
    frame.wait_for_timeout(300)
    labels = frame.evaluate(
        r"""
        () => {
          const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const visible = (element) => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && box.width > 0 && box.height > 0;
          };
          const optionLike = (element) => {
            const role = element.getAttribute('role') || '';
            const classes = typeof element.className === 'string' ? element.className : '';
            const style = getComputedStyle(element);
            const layer = (style.position === 'absolute' || style.position === 'fixed')
              && style.zIndex !== 'auto';
            return role === 'option'
              || /(^|[-_\s])(option|dropdown-item|select-item|menu-item)([-_\s]|$)/i.test(classes)
              || layer;
          };
          return Array.from(document.querySelectorAll('body *'))
            .filter((element) => visible(element) && optionLike(element))
            .map((element) => clean(element.innerText || element.textContent).slice(0, 120))
            .filter(Boolean);
        }
        """
    )
    frame.locator("body").press("Escape")
    return list(dict.fromkeys(str(label) for label in labels if label))


if __name__ == "__main__":
    raise SystemExit(main())
