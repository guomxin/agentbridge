"""Inert reading projections for material already bound to a business draft.

The original evidence, completeness and authorization hashes remain untouched.
Only explicit HTML documents/block fragments are interpreted; ordinary text is
kept verbatim. No source attributes or network-capable elements survive.
"""
from __future__ import annotations

from copy import deepcopy
from html import escape, unescape
from html.parser import HTMLParser
import re


READING_TAGS = frozenset({
    "p", "h2", "br", "ul", "ol", "li", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "strong", "em", "blockquote", "pre", "code", "span",
})
MAX_READING_CHARACTERS = 262144
MAX_READING_NODES = 16384
MAX_READING_DEPTH = 64
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_SKIP = frozenset({"head", "script", "style", "iframe", "frame", "object", "embed", "svg", "math", "template", "noscript"})
_ALIASES = {"b": "strong", "i": "em", "h1": "h2", "h3": "h2", "h4": "h2", "h5": "h2", "h6": "h2",
            "div": "p", "section": "p", "article": "p", "header": "p", "footer": "p", "address": "p", "center": "p"}
_BLOCK = frozenset({"p", "h2", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "blockquote", "pre"})
_DOCUMENT = re.compile(r"<!doctype\s+html\b|<(?:html|head|body)(?:\s|>)", re.I)
_RICH_START = re.compile(r"<(p|h[1-6]|table|ul|ol|blockquote|pre)(?:\s[^<>]*|\s*)>", re.I)
_RICH_END = re.compile(r"</(p|h[1-6]|table|ul|ol|blockquote|pre)\s*>", re.I)
_FALLBACK_NOTICE = "部分材料格式无法转换为阅读视图，已完整保留原始文字。"
_IMAGE_NOTICE = "原材料包含图片；本页只显示图片说明，未读取图片内容。"


def _is_html(value: str) -> bool:
    if _DOCUMENT.search(value):
        return True
    closing = {match.group(1).lower(): match.start() for match in _RICH_END.finditer(value)}
    return any(closing.get(match.group(1).lower(), -1) >= match.end() for match in _RICH_START.finditer(value))


def _candidate(value: str) -> str | None:
    current = value
    for layer in range(3):
        if _is_html(current):
            return current
        if layer == 2:
            break
        decoded = unescape(current)
        if decoded == current:
            break
        current = decoded
    return None


class _ReadingLimit(ValueError):
    pass


class _ReadingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.frames = [("", self.nodes, False)]
        self.count = 0
        self.notices = []

    def _tick(self):
        self.count += 1
        if self.count > MAX_READING_NODES:
            raise _ReadingLimit("node budget")

    def _append(self, node):
        self._tick()
        self.frames[-1][1].append(node)

    def handle_starttag(self, tag, attrs):
        self._tick()
        if len(self.frames) >= MAX_READING_DEPTH:
            raise _ReadingLimit("depth budget")
        # </head> is optional before an explicit body. Do not let that common
        # document form suppress all material. A body nested inside a hidden
        # head child remains suppressed instead of guessing its visibility.
        if tag == "body" and self.frames[-1][0] == "head":
            self.frames.pop()
        if self.frames[-1][2]:
            if tag not in _VOID:
                self.frames.append((tag, [], True))
            return
        # HTML permits omitted ends for common list/table cells. Close only
        # siblings within the current containing list/table, never an outer
        # row when a nested table is beginning.
        closable = {"li": {"li"}, "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"}, "p": {"p"}}.get(tag)
        if closable:
            boundary = {"li": {"ul", "ol"}, "tr": {"table"}, "td": {"tr", "table"},
                        "th": {"tr", "table"}, "p": {"table", "td", "th", "li"}}[tag]
            for index in range(len(self.frames) - 1, 0, -1):
                if self.frames[index][0] in closable:
                    del self.frames[index:]
                    break
                if self.frames[index][0] in boundary:
                    break
        attributes = dict(attrs)
        hidden = ("hidden" in attributes or str(attributes.get("aria-hidden") or "").lower() == "true"
                  or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
                                    str(attributes.get("style") or ""), re.I)))
        skipped = self.frames[-1][2] or tag in _SKIP or hidden
        if skipped:
            if tag not in _VOID:
                self.frames.append((tag, [], True))
            return
        if tag == "img":
            text = str(attributes.get("alt") or "").strip()
            self._append({"tag": "span", "children": [f"[图片：{text}]" if text else "[图片内容未读取]"]})
            if _IMAGE_NOTICE not in self.notices:
                self.notices.append(_IMAGE_NOTICE)
            return
        if tag == "hr":
            self._append({"tag": "br", "children": []})
            return
        mapped = _ALIASES.get(tag, tag)
        if mapped in READING_TAGS:
            node = {"tag": mapped, "children": []}
            if mapped in {"th", "td"}:
                for key in ("colspan", "rowspan"):
                    value = str(attributes.get(key) or "")
                    if value.isascii() and value.isdecimal() and 1 <= int(value) <= 100:
                        node[key] = int(value)
            self._append(node)
            children = node["children"]
        else:
            # Links, forms and unknown wrappers contribute text only. In
            # particular, href/src/action/style/id/event attributes are lost.
            children = self.frames[-1][1]
        if tag not in _VOID:
            self.frames.append((tag, children, False))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.frames) - 1, 0, -1):
            if self.frames[index][0] == tag:
                del self.frames[index:]
                return

    def handle_data(self, data):
        if not data or self.frames[-1][2]:
            return
        if not any(frame[0] in {"pre", "code"} for frame in self.frames):
            data = re.sub(r"\s+", " ", data)
        self._append(data)


def _normalize_blocks(nodes):
    result = []
    for node in nodes:
        if isinstance(node, str):
            result.append(node)
            continue
        node["children"] = _normalize_blocks(node["children"])
        if node["tag"] in {"p", "span", "strong", "em", "code"} and any(
                isinstance(child, dict) and child["tag"] in _BLOCK for child in node["children"]):
            # Source layout divs can contain nested tables/paragraphs. Split
            # phrasing runs instead of producing invalid nested HTML <p>s.
            pending = []
            for child in node["children"]:
                if isinstance(child, dict) and child["tag"] in _BLOCK:
                    if pending:
                        result.append({"tag": node["tag"], "children": pending})
                        pending = []
                    result.append(child)
                else:
                    pending.append(child)
            if pending:
                result.append({"tag": node["tag"], "children": pending})
        else:
            result.append(node)
    return result


def _plain(nodes):
    parts = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
            continue
        tag = node["tag"]
        content = _plain(node["children"])
        if tag == "br":
            parts.append("\n")
        elif tag in {"td", "th"}:
            parts.append(content.strip() + "\t")
        elif tag in _BLOCK:
            parts.append("\n" + content.strip(" \t\n") + "\n")
        else:
            parts.append(content)
    return "".join(parts)


def _valid_reading(reading):
    if not isinstance(reading, dict) or type(reading.get("version")) is not int or reading["version"] != 1:
        return False
    if not isinstance(reading.get("nodes"), list) or not isinstance(reading.get("notices"), list):
        return False
    count = 0
    def visit(nodes, depth):
        nonlocal count
        if depth > MAX_READING_DEPTH:
            return False
        for node in nodes:
            count += 1
            if count > MAX_READING_NODES:
                return False
            if isinstance(node, str):
                continue
            if (not isinstance(node, dict) or not isinstance(node.get("tag"), str) or
                    node["tag"] not in READING_TAGS or not isinstance(node.get("children"), list)):
                return False
            if set(node) - {"tag", "children", "colspan", "rowspan"}:
                return False
            for key in ("colspan", "rowspan"):
                if key in node and (node["tag"] not in {"td", "th"} or type(node[key]) is not int or not 1 <= node[key] <= 100):
                    return False
            if not visit(node["children"], depth + 1):
                return False
        return True
    return all(isinstance(notice, str) for notice in reading["notices"]) and visit(reading["nodes"], 0)


def reading_content(value, existing=None):
    """Return readable text and a closed AST without altering the source value."""
    original = "" if value is None else str(value)
    if _valid_reading(existing) and _reading_text(existing) == original:
        return original, deepcopy(existing)
    fallback = {"version": 1, "nodes": [{"tag": "pre", "children": [original]}], "notices": []}
    if len(original) > MAX_READING_CHARACTERS:
        fallback["notices"].append(_FALLBACK_NOTICE)
        return original, fallback
    candidate = _candidate(original)
    if candidate is None:
        return original, fallback
    try:
        parser = _ReadingParser()
        parser.feed(candidate)
        parser.close()
        nodes = _normalize_blocks(parser.nodes)
        reading = {"version": 1, "nodes": nodes, "notices": parser.notices}
        text = _reading_text(reading)
        if not text.strip():
            raise _ReadingLimit("no visible reading content")
        return text, reading
    except (ValueError, RecursionError, OverflowError):
        fallback["notices"].append(_FALLBACK_NOTICE)
        return original, fallback


def _reading_text(reading):
    nodes = reading["nodes"]
    if (len(nodes) == 1 and isinstance(nodes[0], dict) and nodes[0]["tag"] == "pre"
            and len(nodes[0]["children"]) == 1 and isinstance(nodes[0]["children"][0], str)):
        return nodes[0]["children"][0]
    text = re.sub(r"[ \t]+\n", "\n", _plain(nodes))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def project_review(review):
    """Deep-copy a review's display values; never mutate evidence or its hashes."""
    result = deepcopy(review)
    if not isinstance(result, dict):
        return result
    def fields(items):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "value" in item:
                item["value"], item["reading"] = reading_content(item["value"], item.get("reading"))
    fields(result.get("fields"))
    for group in result.get("groups", []) if isinstance(result.get("groups"), list) else []:
        if isinstance(group, dict):
            fields(group.get("fields"))
    for section in result.get("sections", []) if isinstance(result.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        fields(section.get("fields"))
        if section.get("kind") == "text":
            section["text"], section["reading"] = reading_content(section.get("text"), section.get("reading"))
        if section.get("kind") == "table":
            for row in section.get("rows", []) if isinstance(section.get("rows"), list) else []:
                if not isinstance(row, dict) or not isinstance(row.get("cells"), dict):
                    continue
                readings = row.get("readings") if isinstance(row.get("readings"), dict) else {}
                projected = {}
                for key, value in row["cells"].items():
                    row["cells"][key], projected[key] = reading_content(value, readings.get(key))
                row["readings"] = projected
    return result


def project_draft_state(state):
    result = deepcopy(state)
    if not isinstance(result, dict):
        return result
    ui, preview = result.get("ui"), result.get("preview")
    if isinstance(ui, dict) and isinstance(ui.get("review"), dict):
        ui["review"] = project_review(ui["review"])
    if isinstance(preview, dict) and isinstance(preview.get("summary"), dict):
        preview["summary"] = project_review(preview["summary"])
    if isinstance(result.get("current"), dict):
        result["current"] = project_draft_state(result["current"])
    return result


def render_reading(reading):
    """Render only validated AST nodes; source HTML never reaches this sink."""
    if not _valid_reading(reading):
        return ""
    def nodes_html(nodes):
        pieces = []
        for node in nodes:
            if isinstance(node, str):
                pieces.append(escape(node))
                continue
            tag = node["tag"]
            attributes = "".join(f' {key}="{node[key]}"' for key in ("colspan", "rowspan") if key in node)
            markup = f"<{tag}{attributes}>" + ("" if tag == "br" else nodes_html(node["children"]) + f"</{tag}>")
            if tag == "table":
                markup = '<div class="reading-table" tabindex="0">' + markup + "</div>"
            pieces.append(markup)
        return "".join(pieces)
    notices = "".join(f'<p class="reading-notice">{escape(notice)}</p>' for notice in reading["notices"])
    return '<div class="material-reading">' + nodes_html(reading["nodes"]) + notices + "</div>"
