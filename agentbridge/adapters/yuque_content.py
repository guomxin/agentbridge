from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import unquote
import zlib

from agentbridge.adapters.base import AdapterSessionCheckUnavailable

# Shared by text, OCR, metadata and structured field redaction.
_SECRET_LABEL = (
    r"(?:账号|帐号|用户名|帐密|账密|密码|口令|密钥|"
    r"(?:[A-Za-z0-9]+[_-])*(?:user(?:name)?|password|passwd|pwd|"
    r"access[_ -]?key(?:[_ -]?id)?|secret(?:[_ -]?key)?|api[_ -]?key|token|bearer))"
)
_SECRET_FIELD = re.compile(r"^" + _SECRET_LABEL + r"$", re.I)
_SECRET_PATTERNS = (
    ("credential", re.compile(
        r"(?im)(?<![\w])(" + _SECRET_LABEL + r"[\"']?)([ \t]*[:：=][ \t]*)"
        r'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,，;；<>{}\[\]|]+)'''
    )),
    ("url_credential", re.compile(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@")),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL)),
)


def _redact_table_text(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines(keepends=True)
    secret_columns: set[int] = set()
    categories = []
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            secret_columns = set()
            continue
        cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
        if not cells:
            continue
        if all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells):
            continue
        header_columns = {i for i, cell in enumerate(cells)
                          if _SECRET_FIELD.fullmatch(cell.strip().strip("*` "))}
        # Both header/value tables and two-column field/value tables occur in Lake/OCR.
        targets = set(secret_columns)
        if len(cells) == 2 and 0 in header_columns and 1 not in header_columns:
            targets.add(1)
        for column in targets:
            if (column < len(cells) and column not in header_columns
                    and cells[column].strip() not in {"", "[REDACTED]"}):
                cells[column] = " [REDACTED] "
                categories.append("table_credential")
        if header_columns and not (len(cells) == 2 and header_columns == {0}):
            secret_columns = header_columns
        lines[index] = "|" + "|".join(cells) + "|" + ("\n" if line.endswith("\n") else "")
    return "".join(lines), categories


def lake_to_plain_text(content: str) -> str:
    text, _structure = lake_to_structured_text(content)
    return text


def lake_to_structured_text(content: str) -> tuple[str, dict]:
    parser = _LakeStructuredParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", content)
        structure = {
            "kind": "document",
            "outline": [],
            "tables": [],
            "images": [],
            "attachments": [],
            "links": [],
        }
    else:
        text = parser.text()
        structure = parser.structure()
    return _clean_structured_text(text), structure


def redact_sensitive_text(text: str) -> tuple[str, list[str]]:
    value, categories = _redact_table_text(text)
    for category, pattern in _SECRET_PATTERNS:
        if category == "url_credential":
            value, count = pattern.subn(r"\1[REDACTED]@", value)
        elif category == "private_key":
            value, count = pattern.subn("[REDACTED PRIVATE KEY]", value)
        else:
            def replace(match):
                label = match.group(1).lower()
                categories.append("token" if any(part in label for part in
                    ("token", "key", "secret", "bearer", "密钥")) else "credential")
                return match.group(1) + match.group(2) + "[REDACTED]"
            value = pattern.sub(replace, value)
            continue
        categories.extend([category] * count)
    return value, categories


class _LakeStructuredParser(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "figcaption",
        "figure",
        "p",
        "pre",
        "section",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._table_rows: list[list[str]] | None = None
        self._outline: list[dict] = []
        self._tables: list[dict] = []
        self._images: list[dict] = []
        self._attachments: list[dict] = []
        self._links: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        attributes = {str(name).lower(): str(value or "") for name, value in attrs}
        if lowered in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered in {f"h{level}" for level in range(1, 7)}:
            self._heading_level = int(lowered[1])
            self._heading_parts = []
            self._parts.append("\n" + "#" * self._heading_level + " ")
        elif lowered == "br":
            self._parts.append("\n")
        elif lowered == "li":
            self._parts.append("\n- ")
        elif lowered in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif lowered == "a":
            self._link_href = attributes.get("href") or None
            self._link_parts = []
        elif lowered == "table":
            self._table_rows = []
        elif lowered == "tr" and self._table_rows is not None:
            self._current_row = []
        elif lowered in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
        elif lowered == "card":
            self._handle_card(attributes)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if lowered in {f"h{level}" for level in range(1, 7)}:
            text = _clean_inline_text("".join(self._heading_parts))
            if text and self._heading_level:
                self._outline.append({"level": self._heading_level, "text": text})
            self._heading_level = None
            self._heading_parts = []
            self._parts.append("\n")
        elif lowered == "a":
            label = _clean_inline_text("".join(self._link_parts))
            if self._link_href:
                self._links.append({"label": label or None, "url": self._link_href})
                self._parts.append(f" <{self._link_href}>")
            self._link_href = None
            self._link_parts = []
        elif lowered in {"td", "th"} and self._current_cell is not None:
            if self._current_row is not None:
                self._current_row.append(_clean_inline_text("".join(self._current_cell)))
            self._current_cell = None
        elif lowered == "tr" and self._current_row is not None:
            if self._table_rows is not None:
                self._table_rows.append(self._current_row)
            self._current_row = None
        elif lowered == "table" and self._table_rows is not None:
            rows = self._table_rows
            self._table_rows = None
            if rows:
                column_count = max((len(row) for row in rows), default=0)
                normalized = [row + [""] * (column_count - len(row)) for row in rows]
                self._parts.append("\n" + _render_markdown_rows(normalized) + "\n")
                self._tables.append(
                    {
                        "index": len(self._tables) + 1,
                        "rowCount": len(rows),
                        "columnCount": column_count,
                    }
                )
        elif lowered in self._BLOCK_TAGS or lowered in {"li", "ol", "ul"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._current_cell is not None:
            self._current_cell.append(data)
            return
        self._parts.append(data)
        if self._heading_level:
            self._heading_parts.append(data)
        if self._link_href is not None:
            self._link_parts.append(data)

    def _handle_card(self, attributes: dict[str, str]) -> None:
        name = attributes.get("name", "").casefold()
        card = _decode_lake_card(attributes.get("value"))
        if name == "image":
            ocr_text = "\n".join(
                str(item.get("text") or "").strip()
                for item in card.get("ocrLocations") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            )
            title = str(card.get("title") or card.get("name") or "").strip()
            record = {
                "index": len(self._images) + 1,
                "title": title or None,
                "width": card.get("width") or card.get("originWidth"),
                "height": card.get("height") or card.get("originHeight"),
                "format": card.get("originalType"),
                "size": card.get("size"),
                "ocrText": ocr_text[:4000] or None,
            }
            self._images.append(record)
            label = title or f"图片 {record['index']}"
            self._parts.append(f"\n[图片: {label}]")
            if ocr_text:
                self._parts.append("\n图片文字：" + ocr_text[:4000])
            self._parts.append("\n")
        elif name in {"file", "attachment"}:
            title = str(
                card.get("name") or card.get("title") or card.get("filename") or ""
            ).strip()
            record = {
                "index": len(self._attachments) + 1,
                "name": title or None,
                "size": card.get("size"),
                "format": card.get("type") or card.get("mimeType"),
                "downloadSupported": False,
            }
            self._attachments.append(record)
            self._parts.append(f"\n[附件: {title or record['index']}]\n")

    def text(self) -> str:
        return "".join(self._parts)

    def structure(self) -> dict:
        return {
            "kind": "document",
            "outline": self._outline,
            "tables": self._tables,
            "images": self._images,
            "attachments": self._attachments,
            "links": self._links[:100],
            "linkCount": len(self._links),
        }


def _render_lake_sheet(content: str, *, row_offset: int, max_rows: int) -> dict:
    try:
        outer = json.loads(content)
        binary = str(outer.get("sheet") or "").encode("latin1")
        sheets = json.loads(zlib.decompress(binary).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, zlib.error, json.JSONDecodeError) as exc:
        raise AdapterSessionCheckUnavailable("Yuque sheet content is invalid.") from exc
    if not isinstance(sheets, list):
        raise AdapterSessionCheckUnavailable("Yuque sheet content has no worksheets.")
    sections: list[str] = []
    metadata = []
    for index, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            continue
        raw_data = sheet.get("data")
        if not isinstance(raw_data, dict):
            raw_data = {}
        row_indexes = sorted(
            (int(key) for key in raw_data if str(key).isdigit()),
        )
        selected_indexes = row_indexes[row_offset : row_offset + max_rows]
        column_indexes = sorted(
            {
                int(column)
                for row in raw_data.values()
                if isinstance(row, dict)
                for column in row
                if str(column).isdigit()
            }
        )
        returned_columns = column_indexes[:50]
        rows = []
        secret_columns: set[int] = set()
        redacted_cells = 0
        selected_set = set(selected_indexes)
        for row_index in row_indexes:
            if selected_indexes and row_index > selected_indexes[-1]:
                break
            row = raw_data.get(str(row_index), raw_data.get(row_index, {}))
            values = [
                    _display_sheet_cell(
                        row.get(str(column), row.get(column, {}))
                        if isinstance(row, dict)
                        else {}
                    )
                    for column in returned_columns
                ]
            header_columns = {i for i, value in enumerate(values)
                              if _SECRET_FIELD.fullmatch(value.strip())}
            if header_columns:
                secret_columns = header_columns
            if row_index not in selected_set:
                continue
            for column in secret_columns - header_columns:
                if values[column]:
                    values[column] = "[REDACTED]"
                    redacted_cells += 1
            rows.append(values)
        name = str(sheet.get("name") or f"Sheet {index + 1}")
        headers = [_spreadsheet_column_name(column) for column in returned_columns]
        sections.append(_render_tabular_section(name, headers, rows))
        metadata.append(
            {
                "name": name,
                "rowCount": len(row_indexes),
                "columnCount": len(column_indexes),
                "returnedRows": len(rows),
                "returnedColumns": len(returned_columns),
                "rowOffset": row_offset,
                "hasMore": row_offset + len(rows) < len(row_indexes),
                "columnsTruncated": len(column_indexes) > len(returned_columns),
                "redactedCells": redacted_cells,
            }
        )
    return {
        "text": "\n\n".join(section for section in sections if section).strip(),
        "content_format": "tabular_text_from_lakesheet",
        "structure": {
            "kind": "sheet",
            "sheets": metadata,
            "images": [],
            "attachments": [],
        },
    }


def _render_tabular_section(name: str, headers: list[str], rows: list[list[str]]) -> str:
    title = f"## {name}" if name else "## Sheet"
    if not headers and not rows:
        return title + "\n（无数据）"
    normalized_headers = headers or [
        _spreadsheet_column_name(index)
        for index in range(max((len(row) for row in rows), default=0))
    ]
    width = len(normalized_headers)
    normalized_rows = [row[:width] + [""] * max(0, width - len(row)) for row in rows]
    return title + "\n" + _render_markdown_rows([normalized_headers, *normalized_rows])


def _render_markdown_rows(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    output = [
        "| " + " | ".join(_escape_table_cell(value) for value in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    output.extend(
        "| " + " | ".join(_escape_table_cell(value) for value in row) + " |"
        for row in normalized[1:]
    )
    return "\n".join(output)


def _escape_table_cell(value: Any) -> str:
    return _clean_inline_text(_display_cell_value(value)).replace("|", "\\|")


def _display_sheet_cell(cell: Any) -> str:
    if not isinstance(cell, dict):
        return ""
    for key in ("m", "v", "text", "f"):
        if key in cell:
            return _display_cell_value(cell[key])
    return ""


def _display_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(
            item for item in (_display_cell_value(part) for part in value) if item
        )
    if isinstance(value, dict):
        for key in ("text", "label", "name", "value"):
            if key in value:
                displayed = _display_cell_value(value[key])
                if displayed:
                    return displayed
        return ", ".join(
            item
            for item in (_display_cell_value(part) for part in value.values())
            if item
        )
    return str(value)


def _spreadsheet_column_name(index: int) -> str:
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _decode_lake_card(value: Any) -> dict:
    raw = unquote(str(value or ""))
    if raw.startswith("data:"):
        raw = raw.split(":", 1)[1]
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _clean_structured_text(text: str) -> str:
    value = text.replace("\xa0", " ")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _redact_nested_strings(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        output = []
        categories = []
        for item in value:
            sanitized, found = _redact_nested_strings(item)
            output.append(sanitized)
            categories.extend(found)
        return output, categories
    if isinstance(value, dict):
        output = {}
        categories = []
        for key, item in value.items():
            if _SECRET_FIELD.fullmatch(str(key)) and item is not None:
                sanitized, found = "[REDACTED]", ["credential_field"]
            else:
                sanitized, found = _redact_nested_strings(item)
            output[key] = sanitized
            categories.extend(found)
        return output, categories
    return value, []
