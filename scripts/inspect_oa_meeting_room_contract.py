from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from agentbridge.adapters.seeyon_system import SEEYON_OA_URL
from agentbridge.core.central_service import CentralCapabilityService


_PAGES = (
    "/seeyon/meetingroom.do?method=home",
    "/seeyon/meetingroom.do?method=roomViewComp",
    "/seeyon/meetingroom.do?method=myApps",
    "/seeyon/meetingroom.do?method=appEditor",
)
_KEYWORDS = (
    "appEditor",
    "appView",
    "applyRoom",
    "cacheData",
    "cancel",
    "cancelRoomApp",
    "checkIsOnlyAppOfMeeting",
    "appBeginDate",
    "appEndDate",
    "meetingName",
    "roomApps",
    "delete",
    "meetingAjaxManager",
    "roomApp",
    "roomListInfo",
    "revoke",
)


def _script_urls(html: str, base_url: str) -> list[str]:
    urls = []
    for match in re.finditer(
        r"<script\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidate = urljoin(base_url, match.group(2).strip())
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def _matching_snippets(source: str, *, maximum: int = 12) -> list[str]:
    snippets = []
    for keyword in _KEYWORDS:
        for match in re.finditer(re.escape(keyword), source, flags=re.IGNORECASE):
            start = max(0, match.start() - 300)
            end = min(len(source), match.end() + 700)
            snippet = re.sub(r"\s+", " ", source[start:end]).strip()
            if snippet and snippet not in snippets:
                snippets.append(snippet)
            if len(snippets) >= maximum:
                return snippets
    return snippets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the read-only Seeyon meeting-room page contract."
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--user-subject", required=True)
    args = parser.parse_args()

    service = CentralCapabilityService(home=args.home, base_url=SEEYON_OA_URL)
    session = service.sessions.find(user_subject=args.user_subject, system_id="oa")
    if session is None or session.get("state") != "active":
        raise RuntimeError("The selected OA session is not active")
    state = service.session_states.load(session["session_id"])
    if state is None:
        raise RuntimeError("The selected OA session state is unavailable")

    adapter = service.adapter
    pages = []
    assets: dict[str, dict] = {}
    with service.authentication_worker(session, adapter) as worker:
        worker.restore_session_state(state)
        for path in _PAGES:
            url = urljoin(adapter.base_url, path)
            response = worker.request("GET", url)
            status = int(response.get("status") or 0)
            if status < 200 or status >= 300:
                raise RuntimeError(f"OA meeting-room page returned HTTP {status}: {path}")
            html = str(response.get("text") or "")
            scripts = _script_urls(html, url)
            pages.append(
                {
                    "path": path,
                    "final_url": str(response.get("url") or url),
                    "script_count": len(scripts),
                    "matching_snippets": _matching_snippets(html),
                }
            )
            for script_url in scripts:
                parsed_script = urlparse(script_url)
                if parsed_script.netloc != urlparse(adapter.base_url).netloc:
                    continue
                script_path = parsed_script.path.casefold()
                if not (
                    "meetingroom" in script_path
                    or "/meeting/" in script_path
                    or script_path.endswith("/ajaxstub.js")
                ):
                    continue
                if script_url in assets:
                    continue
                if len(assets) >= 12:
                    continue
                script_response = worker.request("GET", script_url)
                script_status = int(script_response.get("status") or 0)
                source = str(script_response.get("text") or "")
                snippets = _matching_snippets(source)
                if snippets:
                    assets[script_url] = {
                        "status": script_status,
                        "matching_snippets": snippets,
                    }

    print(
        json.dumps(
            {
                "schema_version": "agentbridge.oa_meeting_room_contract_inspection.v1",
                "user_subject": session["user_subject"],
                "pages": pages,
                "assets": assets,
                "safety": {
                    "http_methods": ["GET"],
                    "business_reads": 0,
                    "business_writes": 0,
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
