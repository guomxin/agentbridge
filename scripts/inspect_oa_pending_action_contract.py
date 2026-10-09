from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter
from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.core.config import ConfigStore
from agentbridge.core.session_secrets import SessionStateStore
from agentbridge.core.sessions import SessionRegistry


_PAGE_CONTRACT_SCRIPT = r"""
(expectedAffairId) => {
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const pageAffairId = read(['affairId'])
    || new URLSearchParams(location.search).get('affairId')
    || '';
  return {
    affair_matches: String(pageAffairId) === String(expectedAffairId),
    page_path: location.pathname,
    node_policy: String(window.nodePolicy || ''),
    node_policy_name: String(window.nodePolicyName || ''),
    attitude_codes: Array.from(
      document.querySelectorAll("input[type='radio'][name='attitude']")
    ).map((radio) => String(radio.getAttribute('code') || radio.value || '')),
    identity: {
      summary_id: read(['summaryId']),
      process_id: read(['processId']),
      template_id: read(['templeteId', 'templateId']),
      form_app_id: read(['formAppId']),
      form_record_id: read(['formRecordid', 'formRecordId']),
    },
  };
}
"""


_FRAME_FIELDS_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const containers = Array.from(document.querySelectorAll('[id^="field"][id$="_id"]'))
    .filter((element) => element.closest('[id^="field"][id$="_id"]') === element)
    .map((element) => ({
      id: String(element.id || ''),
      text: clean(element.innerText).slice(0, 1000),
      section_classes: Array.from(element.querySelectorAll(':scope > section'))
        .map((section) => String(section.className || '')),
      selected_text: Array.from(element.querySelectorAll(
        '.cap4-radio-xuanzhong, .cap-icon-danxuan-xuanzhong, '
        + '.cap4-checkbox-xuanzhong, .cap-icon-fuxuan-xuanzhong'
      )).map((marker) => clean(
        marker.closest('.cap4-radio__item, .cap4-checkbox__item')?.innerText
        || marker.parentElement?.innerText
      )).filter(Boolean),
      controls: Array.from(element.querySelectorAll('input,textarea,select')).map((control) => ({
        tag: control.tagName.toLowerCase(),
        id: String(control.id || ''),
        name: String(control.name || ''),
        type: String(control.type || ''),
        value: String(control.value || ''),
        checked: Boolean(control.checked),
        disabled: Boolean(control.disabled),
        read_only: Boolean(control.readOnly),
      })),
      html: String(element.outerHTML || '').slice(0, 5000),
    }))
    .filter((item) => item.text || item.controls.length || item.selected_text.length)
    .slice(0, 300);
  return {
    url: location.href,
    title: document.title,
    matches: containers,
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect one OA pending-action page without executing a write."
    )
    parser.add_argument("--home", required=True)
    parser.add_argument("--affair-id", required=True)
    parser.add_argument("--user-subject")
    args = parser.parse_args()

    home = Path(args.home).resolve()
    profile = ConfigStore(home).load_system("oa")
    registry = SessionRegistry(home / "agentbridge.db", home / "profiles")
    sessions = registry.list_active(system_id="oa")
    if args.user_subject:
        sessions = [
            item for item in sessions if item["user_subject"] == args.user_subject
        ]
    if len(sessions) != 1:
        raise RuntimeError("exactly one matching active OA session is required")

    session = sessions[0]
    state = SessionStateStore(home / "session-secrets").load(session["session_id"])
    if state is None:
        raise RuntimeError("the active OA session has no encrypted browser state")

    adapter = SeeyonCentralAdapter(base_url=profile.base_url)
    blocked_writes: list[dict] = []
    with CentralBrowserWorker(
        profile_path=session["profile_path"],
        allowed_origins={adapter.origin},
        headless=True,
    ) as worker:
        worker.restore_session_state(state)

        def guard_route(route) -> None:
            request = route.request
            parsed = urlparse(str(request.url or ""))
            if (
                str(request.method or "").upper() == "POST"
                and parsed.path.endswith("/collaboration/collaboration.do")
            ):
                blocked_writes.append(
                    {
                        "method": "POST",
                        "endpoint": "/seeyon/collaboration/collaboration.do",
                    }
                )
                route.abort()
                return
            route.continue_()

        worker._context.route("**/*", guard_route)
        source, detail = adapter.resolve_workflow_detail(
            worker,
            collection="pending",
            affair_id=args.affair_id,
        )
        page_contract = worker.page.evaluate(_PAGE_CONTRACT_SCRIPT, args.affair_id)
        frames = []
        for frame in list(worker.page.frames):
            if "/cap4/" not in str(frame.url or ""):
                continue
            frames.append(frame.evaluate(_FRAME_FIELDS_SCRIPT))

    if blocked_writes:
        raise RuntimeError("an OA collaboration write request was attempted and blocked")

    print(
        json.dumps(
            {
                "schema_version": "agentbridge.oa_pending_action_contract_inspection.v1",
                "user_subject": session["user_subject"],
                "source": source,
                "detail": detail,
                "page_contract": page_contract,
                "cap4_frames": frames,
                "safety": {
                    "collaboration_write_requests": 0,
                    "authorizations_created": 0,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
