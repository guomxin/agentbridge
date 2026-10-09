from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urljoin

from agentbridge.adapters.seeyon_system import SEEYON_OA_URL
from agentbridge.core.central_service import CentralCapabilityService


ADDRESSBOOK_ENTRY = "addressbook.do?method=homeEntry"


def _visible_frames(page) -> list[dict]:
    frames = []
    for frame in page.frames:
        try:
            summary = frame.evaluate(
                r"""
                () => ({
                  title: document.title || "",
                  links: Array.from(document.querySelectorAll("a")).slice(0, 500).map((a) => ({
                    id: a.id || "",
                    text: (a.innerText || a.textContent || "").replace(/\s+/g, " ").trim(),
                    href: a.getAttribute("href") || "",
                    onclick: a.getAttribute("onclick") || "",
                    title: a.getAttribute("title") || "",
                  })).filter((item) =>
                    (item.text || item.href || item.onclick) &&
                    !/showV3XMemberCard/i.test(item.href + item.onclick)
                  ),
                  inputs: Array.from(document.querySelectorAll("input, select")).slice(0, 200).map((el) => ({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || "",
                    name: el.getAttribute("name") || "",
                    type: el.getAttribute("type") || "",
                    value: ["accountId", "showAccountOrDept", "showType", "frameUrl",
                      "deptId", "deptIds", "otId", "sysId", "mem", "pageSize"
                    ].includes(el.id || el.name || "") ? (el.value || "") : "",
                    options: el.tagName === "SELECT" ? Array.from(el.options).map((o) => ({
                      text: (o.textContent || "").trim(),
                      value: o.value || "",
                    })) : [],
                  })).filter((item) => item.tag === "select" || item.value !== ""),
                  trees: ["accountTree", "tree", "teamTree"].flatMap((treeId) => {
                    try {
                      const tree = window.jQuery?.fn?.zTree?.getZTreeObj(treeId);
                      if (!tree) return [];
                      return tree.transformToArray(tree.getNodes()).slice(0, 300).map((node) => ({
                        treeId,
                        id: String(node.id ?? ""),
                        pId: String(node.pId ?? ""),
                        name: String(node.name ?? ""),
                        type: String(node.type ?? node.orgType ?? ""),
                        accountId: String(node.accountId ?? ""),
                        deptId: String(node.deptId ?? ""),
                        teamId: String(node.teamId ?? ""),
                        isParent: Boolean(node.isParent),
                      }));
                    } catch (_error) {
                      return [];
                    }
                  }),
                  tables: Array.from(document.querySelectorAll("table")).map((table) => ({
                    id: table.id || "",
                    className: table.className || "",
                    headers: Array.from(table.querySelectorAll("th")).map((cell) =>
                      (cell.innerText || cell.textContent || "").replace(/\s+/g, " ").trim()
                    ),
                    rowCount: table.querySelectorAll("tr").length,
                    firstDataRow: (() => {
                      const row = Array.from(table.querySelectorAll("tr")).find((candidate) =>
                        candidate.querySelector('input[name="id"], a[href*="showV3XMemberCard"]')
                      );
                      return row ? {
                        cellCount: row.querySelectorAll("td").length,
                        cellClasses: Array.from(row.querySelectorAll("td")).map((cell) => cell.className || ""),
                        inputNames: Array.from(row.querySelectorAll("input")).map((input) => input.name || input.id || ""),
                      } : null;
                    })(),
                  })).filter((table) => table.headers.length || table.firstDataRow),
                })
                """
            )
        except Exception:
            continue
        frames.append(
            {
                "url": frame.url,
                "title": summary.get("title"),
                "links": summary.get("links") or [],
                "inputs": summary.get("inputs") or [],
                "trees": summary.get("trees") or [],
                "tables": summary.get("tables") or [],
            }
        )
    return frames


def _frame_urls(page) -> list[dict]:
    result = []
    for frame in page.frames:
        try:
            fields = frame.evaluate(
                """
                () => Object.fromEntries(
                  ["accountId", "deptId", "deptIds", "otId", "sysId", "mem", "pageSize"]
                    .map((name) => [name, document.getElementById(name)?.value ??
                      document.querySelector(`[name="${name}"]`)?.value ?? ""])
                    .filter(([, value]) => value !== "")
                )
                """
            )
        except Exception:
            fields = {}
        result.append({"url": frame.url, "fields": fields})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect the read-only Seeyon address-book page contract."
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--user-subject", required=True)
    args = parser.parse_args()

    service = CentralCapabilityService(home=args.home, base_url=SEEYON_OA_URL)
    session = service.sessions.find(user_subject=args.user_subject, system_id="oa")
    if session is None or session.get("state") != "active":
        raise RuntimeError("The selected OA session is not active")
    adapter = service.adapter
    state = service.session_states.load(session["session_id"])
    if state is None:
        raise RuntimeError("The selected OA session state is unavailable")

    with service.authentication_worker(session, adapter) as worker:
        worker.restore_session_state(state)
        page = worker.goto(urljoin(adapter.base_url, ADDRESSBOOK_ENTRY), timeout_seconds=60)
        page.wait_for_timeout(1500)
        output = {
            "page_url": page.url,
            "frame_count": len(page.frames),
            "frames": _visible_frames(page),
        }
        department_tree = next(
            (frame for frame in page.frames if "method=treeDept" in frame.url),
            None,
        )
        if department_tree is not None:
            department_tree.get_by_text("人工智能研发中心", exact=True).click()
            page.wait_for_timeout(800)
            output["after_department_click"] = _frame_urls(page)

        account_id = next(
            (
                frame_data.get("fields", {}).get("accountId")
                for frame_data in _frame_urls(page)
                if frame_data.get("fields", {}).get("accountId")
            ),
            None,
        )
        if not account_id:
            raise RuntimeError("The OA address book did not expose an account ID")

        output["group_pages"] = {}
        for addressbook_type in (2, 4, 3, 6):
            page.goto(
                urljoin(
                    adapter.base_url,
                    f"addressbook.do?method=home&addressbookType={addressbook_type}",
                ),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(1000)
            group_output = {
                "page_url": page.url,
                "frames": _visible_frames(page),
            }
            tree_frame = next(
                (
                    frame
                    for frame in page.frames
                    if "method=treeOwnTeam" in frame.url
                    or "method=treeSysTeam" in frame.url
                ),
                None,
            )
            if tree_frame is not None:
                nodes = tree_frame.locator('a[id^="accountTree_"]')
                if nodes.count() > 1:
                    nodes.nth(1).click()
                    page.wait_for_timeout(600)
                    group_output["after_first_group_click"] = _frame_urls(page)
            output["group_pages"][str(addressbook_type)] = group_output

        page.goto(
            urljoin(
                adapter.base_url,
                "addressbook.do?method=initList&addressbookType=1&showType=list&"
                f"accountId={account_id}&pageSize=500",
            ),
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(500)
        output["page_size_probe"] = {
            "frames": _visible_frames(page),
        }
        service.session_states.save(session["session_id"], worker.capture_session_state())

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
