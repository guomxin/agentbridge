from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse
from functools import wraps

from bscli.adapters.base import (
    AdapterLoginContractMismatch,
    AdapterLoginRequired,
    AdapterSessionCheckUnavailable,
)
from bscli.adapters.yuque_content import (
    _display_cell_value,
    _redact_nested_strings,
    _render_lake_sheet,
    _render_tabular_section,
    lake_to_plain_text,
    lake_to_structured_text,
    redact_sensitive_text,
)
from bscli.core.capability import CapabilityRegistry, CapabilitySpec


YUQUE_SYSTEM_ID = "yuque"
YUQUE_ADAPTER_ID = "yuque-central"
YUQUE_SYSTEM_NAME = "部门信息库"

YUQUE_PUBLIC_BOOKS_CAPABILITY = "yuque.public_books.list"
YUQUE_DOCUMENT_CATALOG_CAPABILITY = "yuque.document.catalog"
YUQUE_DOCUMENT_SEARCH_CAPABILITY = "yuque.document.search"
YUQUE_DOCUMENT_READ_CAPABILITY = "yuque.document.read"

_BOOK_SELECTOR_SCHEMA = {
    "book": {
        "type": "string",
        "description": (
            "Knowledge-base name, slug, or numeric id. Omit it to search or list "
            "across knowledge bases in the organization public area only."
        ),
    },
}

class YuqueLoginRequired(AdapterLoginRequired):
    pass


class YuqueLoginContractMismatch(AdapterLoginContractMismatch):
    pass


class YuqueSessionCheckUnavailable(AdapterSessionCheckUnavailable):
    pass


def _public_output(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        result["searchScope"] = {"kind": "organization_public_area",
                                 "organizationId": self.organization_id,
                                 "includesPrivateSpaces": False}
        original = result
        result, categories = _redact_nested_strings(result)
        locations = [key for key in original if original[key] != result[key]]
        redaction = result.setdefault("redaction", {})
        if redaction.get("applied"):
            locations.extend(["content", "structure"])
        redaction["locations"] = sorted(set(locations))
        redaction.update({"applied": bool(categories) or redaction.get("applied", False),
                          "count": len(categories) + redaction.get("count", 0),
                          "categories": sorted(set(categories + redaction.get("categories", []))),
                          "detection": "known_patterns_only; no match does not prove absence of secrets"})
        return result
    return wrapped


class YuqueCentralAdapter:
    def __init__(self, *, base_url: str, organization_id: int) -> None:
        parsed = urlparse(str(base_url or ""))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Yuque base URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Yuque base URL is invalid")
        if parsed.path not in {"", "/"}:
            raise ValueError("Yuque base URL must not include a path")
        hostname = (parsed.hostname or "").lower()
        if hostname != "yuque.com" and not hostname.endswith(".yuque.com"):
            raise ValueError("Yuque base URL must use a yuque.com host")
        if not isinstance(organization_id, int) or organization_id <= 0:
            raise ValueError("Yuque organization id must be a positive integer")
        self.base_url = f"https://{parsed.netloc.lower()}/"
        self.origin = f"https://{parsed.netloc.lower()}"
        self.organization_id = organization_id
        self.organization_login = hostname.removesuffix(".yuque.com")

    def authentication_contract(self) -> dict:
        return {
            "system_id": YUQUE_SYSTEM_ID,
            "system_name": YUQUE_SYSTEM_NAME,
            "origin": self.origin,
            "page_fingerprint": "yuque-interactive-login-v1",
            "authentication_mode": "interactive_browser",
            "fields": [],
            "interactive": {
                "entry_url": self._url(
                    "/login?"
                    + urlencode(
                        {
                            "org": self.organization_login,
                            "goto": self.base_url,
                        }
                    )
                ),
                "viewport": {"width": 430, "height": 760},
                "requires_human_verification": True,
            },
        }

    def begin_interactive_login(self, worker, *, timeout_seconds: float) -> dict:
        entry_url = self.authentication_contract()["interactive"]["entry_url"]
        worker.goto(entry_url, timeout_seconds=timeout_seconds)
        return {"url": worker.page_url, "transport": "central_interactive_browser"}

    def authenticate(self, _worker, _credentials: dict, *, timeout_seconds: float) -> dict:
        del timeout_seconds
        raise YuqueLoginContractMismatch(
            "Yuque requires the interactive browser authentication contract."
        )

    def probe_session(self, worker) -> dict:
        payload = self._request_json(worker, "GET", "/api/mine", login_probe=True)
        principal = payload.get("data")
        if not isinstance(principal, dict):
            raise YuqueLoginContractMismatch("Yuque session response has no principal.")
        observed = str(
            principal.get("publicName")
            or principal.get("name")
            or principal.get("login")
            or ""
        ).strip()
        if not observed:
            raise YuqueLoginContractMismatch(
                "Yuque session response has no verifiable principal."
            )
        if principal.get("isInCurrentOrg") is False:
            raise YuqueLoginRequired(
                "The authenticated Yuque account is not a member of this organization."
            )
        return {
            "authenticated": True,
            "observed_principal_ref": observed,
            "principal": {
                "id": str(principal.get("id") or ""),
                "login": str(principal.get("login") or ""),
                "name": observed,
                "organization_member": principal.get("isInCurrentOrg"),
            },
            "template_count": None,
            "transport": "central_browser_cookie",
        }

    def invoke_capability(self, capability_name: str, worker, arguments: dict) -> dict:
        if capability_name == YUQUE_PUBLIC_BOOKS_CAPABILITY:
            return self.list_public_books(worker)
        if capability_name == YUQUE_DOCUMENT_CATALOG_CAPABILITY:
            return self.list_documents(worker, arguments)
        if capability_name == YUQUE_DOCUMENT_SEARCH_CAPABILITY:
            return self.search_documents(worker, arguments)
        if capability_name == YUQUE_DOCUMENT_READ_CAPABILITY:
            return self.read_document(worker, arguments)
        raise KeyError(f"unsupported Yuque capability: {capability_name}")

    @_public_output
    def list_public_books(self, worker) -> dict:
        public_area, books = self._public_area(worker)
        return {
            "publicArea": public_area,
            "count": len(books),
            "items": books,
        }

    @_public_output
    def list_documents(self, worker, arguments: dict) -> dict:
        public_area, books = self._public_area(worker)
        selector = str(arguments.get("book") or "").strip()
        selected_books = [_select_book(books, selector)] if selector else books
        keyword = str(arguments.get("keyword") or "").strip()
        document_type = _document_type_option(arguments.get("document_type"))
        updated_after = _datetime_option(
            arguments.get("updated_after"), option="updated_after", upper=False
        )
        updated_before = _datetime_option(
            arguments.get("updated_before"), option="updated_before", upper=True
        )
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=1000)
        limit = _bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=500)
        sort = str(arguments.get("sort") or "updated_desc").strip().lower()
        if sort not in {"updated_desc", "updated_asc", "title_asc", "title_desc"}:
            raise ValueError(
                "sort must be updated_desc, updated_asc, title_asc, or title_desc"
            )

        normalized = []
        for book in selected_books:
            normalized.extend(self._list_book_documents(worker, book))
        if keyword:
            folded = keyword.casefold()
            normalized = [
                item for item in normalized if folded in item["title"].casefold()
            ]
        if document_type:
            normalized = [
                item
                for item in normalized
                if item["type"].casefold() == document_type.casefold()
            ]
        if updated_after:
            normalized = [
                item
                for item in normalized
                if (stamp := _parse_api_datetime(item.get("updatedAt"))) is not None
                and stamp >= updated_after
            ]
        if updated_before:
            normalized = [
                item
                for item in normalized
                if (stamp := _parse_api_datetime(item.get("updatedAt"))) is not None
                and stamp <= updated_before
            ]

        if sort.startswith("updated_"):
            normalized.sort(
                key=lambda item: _parse_api_datetime(item.get("updatedAt"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=sort.endswith("desc"),
            )
        else:
            normalized.sort(
                key=lambda item: item["title"].casefold(),
                reverse=sort.endswith("desc"),
            )
        offset = (page - 1) * limit
        items = normalized[offset : offset + limit]
        for item in items:
            item["sourceUrl"] = self._document_url(item, item["book"])
        selected_book = selected_books[0] if selector else None
        return {
            "scope": "book" if selected_book else "all_books",
            "publicArea": public_area,
            "book": selected_book,
            "booksScanned": len(selected_books),
            "keyword": keyword or None,
            "documentType": document_type,
            "updatedAfter": arguments.get("updated_after") or None,
            "updatedBefore": arguments.get("updated_before") or None,
            "sort": sort,
            "page": page,
            "limit": limit,
            "total": len(normalized),
            "count": len(items),
            "hasMore": offset + len(items) < len(normalized),
            "items": items,
        }

    @_public_output
    def search_documents(self, worker, arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        public_area, books = self._public_area(worker)
        selector = str(arguments.get("book") or "").strip()
        selected_book = _select_book(books, selector) if selector else None
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=1000)
        limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
        document_type = _document_type_option(arguments.get("document_type"))
        scope = public_area["login"]
        if selected_book:
            scope = f"{scope}/{selected_book['slug']}"
        params = {
            "p": page,
            "q": query,
            "limit": limit,
            "sence": "modal",
            "type": "content",
            "scope": scope,
            "tab": "book",
        }
        payload = self._request_json(
            worker,
            "GET",
            f"/api/zsearch?{urlencode(params)}",
        )
        result = payload.get("data")
        if not isinstance(result, dict) or not isinstance(result.get("hits"), list):
            raise YuqueSessionCheckUnavailable(
                "Yuque search did not return a hit list."
            )
        items = []
        for item in result["hits"]:
            if not isinstance(item, dict):
                continue
            book = _book_for_search_hit(item, books, fallback=selected_book)
            normalized = _normalize_search_hit(item, book)
            normalized["sourceUrl"] = self._document_url(normalized, book)
            if document_type and normalized["type"].casefold() != document_type.casefold():
                continue
            items.append(normalized)
        total = int(result.get("totalHits") or result.get("numHits") or len(items))
        return {
            "query": query,
            "scope": "book" if selected_book else "all_books",
            "book": selected_book,
            "booksSearched": 1 if selected_book else len(books),
            "documentType": document_type,
            "page": page,
            "limit": limit,
            "count": len(items),
            "total": total,
            "hasMore": page * limit < total,
            "snippetPolicy": "omitted_to_prevent_incidental_secret_disclosure",
            "items": items,
        }

    @_public_output
    def read_document(self, worker, arguments: dict) -> dict:
        selector = str(arguments.get("document") or "").strip()
        if not selector:
            raise ValueError("document is required")
        book_selector = str(arguments.get("book") or "").strip()
        if book_selector:
            book = self._resolve_book(worker, book_selector)
            document = self._resolve_document(worker, book, selector)
        else:
            book, document = self._resolve_document_across_books(worker, selector)
        params = {
            "include_contributors": "true",
            "include_like": "true",
            "include_hits": "true",
            "merge_dynamic_data": "false",
            "book_id": book["id"],
        }
        payload = self._request_json(
            worker,
            "GET",
            f"/api/docs/{quote(document['slug'], safe='')}?{urlencode(params)}",
        )
        raw = payload.get("data")
        if not isinstance(raw, dict):
            raise YuqueSessionCheckUnavailable("Yuque document response is invalid.")
        row_offset = _bounded_int(
            arguments.get("row_offset"), default=0, minimum=0, maximum=100_000
        )
        max_rows = _bounded_int(
            arguments.get("max_rows"), default=100, minimum=1, maximum=500
        )
        rendered = self._render_document(
            worker,
            raw,
            row_offset=row_offset,
            max_rows=max_rows,
        )
        sanitized_text, redactions = redact_sensitive_text(rendered["text"])
        sanitized_structure, structure_redactions = _redact_nested_strings(
            rendered["structure"]
        )
        redactions.extend(structure_redactions)
        redactions.extend(["table_credential"] * sum(
            sheet.get("redactedCells", 0) for sheet in rendered["structure"].get("sheets", [])
        ))
        max_chars = _bounded_int(
            arguments.get("max_chars"),
            default=12_000,
            minimum=500,
            maximum=50_000,
        )
        truncated = len(sanitized_text) > max_chars
        if truncated:
            sanitized_text = sanitized_text[:max_chars].rstrip()
        return {
            "document": {
                **_normalize_document_summary(raw, book),
                "sourceUrl": self._document_url({**document, **raw}, book),
                "author": _normalize_person(raw.get("user")),
                "lastEditor": _normalize_person(raw.get("last_editor")),
                "contributors": [
                    person
                    for item in raw.get("contributors") or []
                    if isinstance(item, dict)
                    if (person := _normalize_person(item)) is not None
                ],
                "wordCount": raw.get("word_count"),
                "contentUpdatedAt": raw.get("content_updated_at"),
                "createdAt": raw.get("created_at"),
                "updatedAt": raw.get("updated_at"),
            },
            "content": sanitized_text,
            "contentFormat": rendered["content_format"],
            "structure": sanitized_structure,
            "rowOffset": row_offset,
            "maxRows": max_rows,
            "truncated": truncated,
            "maxChars": max_chars,
            "redaction": {
                "applied": bool(redactions),
                "count": len(redactions),
                "categories": sorted(set(redactions)),
                "policy": "likely credentials and secrets are never returned to the agent",
            },
        }

    def _list_book_documents(self, worker, book: dict) -> list[dict]:
        payload = self._request_json(
            worker,
            "GET",
            f"/api/docs?{urlencode({'book_id': book['id']})}",
        )
        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            raise YuqueSessionCheckUnavailable(
                "Yuque document catalog did not return a list."
            )
        return [
            _normalize_document_summary(item, book)
            for item in raw_items
            if isinstance(item, dict)
        ]

    def _resolve_document_across_books(
        self, worker, selector: str
    ) -> tuple[dict, dict]:
        _public_area, books = self._public_area(worker)
        folded = selector.casefold()
        exact: list[tuple[dict, dict]] = []
        partial: list[tuple[dict, dict]] = []
        for book in books:
            for item in self._list_book_documents(worker, book):
                if (
                    selector == str(item["id"])
                    or folded == item["slug"].casefold()
                    or folded == item["title"].casefold()
                ):
                    exact.append((book, item))
                elif folded in item["title"].casefold():
                    partial.append((book, item))
        candidates = exact or partial
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"Yuque document was not found: {selector}")
        raise ValueError(
            "Yuque document selector is ambiguous: "
            + ", ".join(
                f"{item['title']} ({book['name']})" for book, item in candidates[:10]
            )
        )

    def _render_document(
        self,
        worker,
        raw: dict,
        *,
        row_offset: int,
        max_rows: int,
    ) -> dict:
        document_type = str(raw.get("type") or "Doc").casefold()
        document_format = str(raw.get("format") or "lake").casefold()
        content = str(raw.get("content") or raw.get("body") or "")
        if document_type == "sheet" or document_format == "lakesheet":
            return _render_lake_sheet(
                content,
                row_offset=row_offset,
                max_rows=max_rows,
            )
        if document_type == "table" or document_format == "laketable":
            return self._render_lake_table(
                worker,
                raw,
                content,
                row_offset=row_offset,
                max_rows=max_rows,
            )
        text, structure = lake_to_structured_text(content)
        return {
            "text": text,
            "content_format": "structured_text_from_lake",
            "structure": structure,
        }

    def _render_lake_table(
        self,
        worker,
        raw: dict,
        content: str,
        *,
        row_offset: int,
        max_rows: int,
    ) -> dict:
        try:
            model = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise YuqueSessionCheckUnavailable(
                "Yuque data table content is invalid."
            ) from exc
        sheets = model.get("sheet")
        if not isinstance(sheets, list) or not sheets:
            raise YuqueSessionCheckUnavailable("Yuque data table has no sheets.")
        sections: list[str] = []
        metadata = []
        for index, sheet in enumerate(sheets):
            if not isinstance(sheet, dict):
                continue
            columns = [item for item in sheet.get("columns") or [] if isinstance(item, dict)]
            sheet_id = str(model.get("sheetId") or sheet.get("id") or "").strip()
            if not sheet_id:
                continue
            params = {
                "docId": raw.get("id"),
                "docType": "Doc",
                "limit": max_rows,
                "offset": row_offset,
                "sheetId": sheet_id,
            }
            response = self._request_json(
                worker,
                "GET",
                "/api/modules/table/doc/TableRecordController/show?"
                + urlencode(params),
            )
            records = response.get("records")
            if not isinstance(records, list):
                raise YuqueSessionCheckUnavailable(
                    "Yuque data table did not return rows."
                )
            headers = [str(item.get("name") or f"Column {offset + 1}") for offset, item in enumerate(columns)]
            rows = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                try:
                    values = json.loads(str(record.get("data") or "{}"))
                except json.JSONDecodeError:
                    values = {}
                rows.append(
                    [
                        _display_cell_value(
                            (values.get(str(column.get("id") or "")) or {}).get("value")
                        )
                        for column in columns
                    ]
                )
            name = str(sheet.get("name") or f"Sheet {index + 1}")
            sections.append(_render_tabular_section(name, headers, rows))
            metadata.append(
                {
                    "name": name,
                    "columnCount": len(headers),
                    "columns": headers,
                    "returnedRows": len(rows),
                    "rowOffset": row_offset,
                    "hasMore": bool(response.get("hasMore")),
                }
            )
        return {
            "text": "\n\n".join(section for section in sections if section).strip(),
            "content_format": "tabular_text_from_laketable",
            "structure": {
                "kind": "table",
                "sheets": metadata,
                "images": [],
                "attachments": [],
            },
        }

    def _resolve_book(self, worker, selector: Any) -> dict:
        _public_area, books = self._public_area(worker)
        return _select_book(books, str(selector or "共享文档"))
    def _resolve_document(self, worker, book: dict, selector: str) -> dict:
        catalog = self.list_documents(
            worker,
            {"book": str(book["id"]), "limit": 500},
        )["items"]
        folded = selector.casefold()
        exact = [
            item
            for item in catalog
            if selector == str(item["id"])
            or folded == item["slug"].casefold()
            or folded == item["title"].casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        partial = [item for item in catalog if folded in item["title"].casefold()]
        if len(partial) == 1:
            return partial[0]
        if not exact and not partial:
            raise ValueError(f"Yuque document was not found: {selector}")
        candidates = exact or partial
        raise ValueError(
            "Yuque document selector is ambiguous: "
            + ", ".join(item["title"] for item in candidates[:10])
        )

    def _public_area(self, worker) -> tuple[dict, list[dict]]:
        payload = self._request_json(
            worker,
            "GET",
            "/api/modules/org_wiki/wiki/show?"
            + urlencode({"organizationId": self.organization_id}),
        )
        wiki = payload.get("wiki")
        layouts = payload.get("layouts")
        if not isinstance(wiki, dict) or not isinstance(layouts, list):
            raise YuqueSessionCheckUnavailable(
                "Yuque public-area response is invalid."
            )
        books: list[dict] = []
        for layout in layouts:
            if not isinstance(layout, dict):
                continue
            for placement in layout.get("placements") or []:
                if not isinstance(placement, dict):
                    continue
                for block in placement.get("blocks") or []:
                    if not isinstance(block, dict) or block.get("type") != "bookStacks":
                        continue
                    for stack in block.get("data") or []:
                        if not isinstance(stack, dict):
                            continue
                        for book in stack.get("books") or []:
                            if isinstance(book, dict):
                                books.append(_normalize_book(book))
        public_area = {
            "id": str(wiki.get("id") or ""),
            "name": str(wiki.get("name") or "公共区"),
            "login": str(wiki.get("login") or ""),
            "organizationId": self.organization_id,
        }
        if not public_area["login"]:
            raise YuqueLoginContractMismatch(
                "Yuque public area has no stable login identifier."
            )
        for book in books:
            book["publicAreaLogin"] = public_area["login"]
        return public_area, books

    def _request_json(
        self,
        worker,
        method: str,
        path: str,
        *,
        login_probe: bool = False,
    ) -> dict:
        response = worker.request(method, self._url(path))
        final_url = str(response.get("url") or "")
        if response["status"] in {401, 403} or "/login" in urlparse(final_url).path:
            raise YuqueLoginRequired("Yuque login is missing or expired.")
        if response["status"] < 200 or response["status"] >= 300:
            if login_probe and response["status"] in {302, 303, 307, 308}:
                raise YuqueLoginRequired("Yuque login is missing or expired.")
            raise YuqueSessionCheckUnavailable(
                f"Yuque API returned HTTP {response['status']}."
            )
        payload = response.get("json")
        if not isinstance(payload, dict):
            raise YuqueSessionCheckUnavailable("Yuque API did not return JSON.")
        return payload

    def _document_url(self, document: dict, book: dict) -> str | None:
        supplied = str(document.get("url") or "").strip()
        if not supplied:
            area = book.get("publicAreaLogin")
            slug = document.get("slug")
            if not area or not book.get("slug") or not slug:
                return None
            supplied = "/" + "/".join(quote(str(part), safe="") for part in (area, book["slug"], slug))
        target = urljoin(self.base_url, supplied)
        parsed = urlparse(target)
        if (parsed.scheme != "https" or parsed.netloc != urlparse(self.base_url).netloc
                or parsed.username or parsed.password):
            return None
        return target

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))


def build_yuque_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    specs = (
        CapabilitySpec(
            name=YUQUE_PUBLIC_BOOKS_CAPABILITY,
            version="0.1.0",
            description="List knowledge bases in the authenticated Yuque public area.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effect="read",
            adapter=YUQUE_ADAPTER_ID,
            workflow="yuque-public-books-v1",
        ),
        CapabilitySpec(
            name=YUQUE_DOCUMENT_CATALOG_CAPABILITY,
            version="0.2.0",
            description=(
                "List, filter, sort, and page documents in the organization public area of Yuque "
                "knowledge bases or one explicitly selected base."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_BOOK_SELECTOR_SCHEMA,
                    "keyword": {"type": "string"},
                    "document_type": {
                        "type": "string",
                        "enum": ["all", "doc", "sheet", "table"],
                    },
                    "updated_after": {"type": "string"},
                    "updated_before": {"type": "string"},
                    "sort": {
                        "type": "string",
                        "enum": [
                            "updated_desc",
                            "updated_asc",
                            "title_asc",
                            "title_desc",
                        ],
                    },
                    "page": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=YUQUE_ADAPTER_ID,
            workflow="yuque-document-catalog-v2",
        ),
        CapabilitySpec(
            name=YUQUE_DOCUMENT_SEARCH_CAPABILITY,
            version="0.2.0",
            description=(
                "Search knowledge bases in the organization public area; page and read relevant candidates to locate a target, while omitting "
                "server snippets that may incidentally expose credentials."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_BOOK_SELECTOR_SCHEMA,
                    "query": {"type": "string"},
                    "document_type": {
                        "type": "string",
                        "enum": ["all", "doc", "sheet", "table"],
                    },
                    "page": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=YUQUE_ADAPTER_ID,
            workflow="yuque-document-search-v2",
        ),
        CapabilitySpec(
            name=YUQUE_DOCUMENT_READ_CAPABILITY,
            version="0.2.0",
            description=(
                "Read one selected Yuque Doc, Sheet, or Table as sanitized structured "
                "text with outline, table, image/OCR, link, and attachment metadata."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_BOOK_SELECTOR_SCHEMA,
                    "document": {
                        "type": "string",
                        "description": "Document title, slug, or numeric id.",
                    },
                    "row_offset": {"type": "integer"},
                    "max_rows": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["document"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=YUQUE_ADAPTER_ID,
            workflow="yuque-document-read-v2",
        ),
    )
    for spec in specs:
        registry.register(spec)
    return registry

def _select_book(books: list[dict], selector: str) -> dict:
    query = str(selector or "").strip()
    exact = [
        book
        for book in books
        if query == str(book["id"])
        or query.casefold() == book["slug"].casefold()
        or query.casefold() == book["name"].casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [book for book in books if query.casefold() in book["name"].casefold()]
    if len(partial) == 1:
        return partial[0]
    if not exact and not partial:
        raise ValueError(f"Yuque knowledge base was not found: {query}")
    raise ValueError(
        "Yuque knowledge-base selector is ambiguous: "
        + ", ".join(book["name"] for book in (exact or partial)[:10])
    )


def _book_for_search_hit(
    item: dict, books: list[dict], *, fallback: dict | None
) -> dict:
    if fallback:
        return fallback
    path = urlparse(str(item.get("url") or "")).path
    parts = [part for part in path.split("/") if part]
    book_slug = parts[-2] if len(parts) >= 2 else ""
    for book in books:
        if book_slug and book["slug"].casefold() == book_slug.casefold():
            return book
    book_name = str(item.get("book_name") or "").strip()
    for book in books:
        if book_name and book["name"].casefold() == book_name.casefold():
            return book
    return {"id": "", "slug": book_slug, "name": book_name or "未知知识库"}


def _document_type_option(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    if not normalized or normalized == "all":
        return None
    mapping = {"doc": "Doc", "sheet": "Sheet", "table": "Table"}
    if normalized not in mapping:
        raise ValueError("document_type must be all, doc, sheet, or table")
    return mapping[normalized]


def _datetime_option(value: Any, *, option: str, upper: bool) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            stamp = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            if upper:
                stamp = stamp.replace(hour=23, minute=59, second=59, microsecond=999999)
            return stamp
        return _parse_api_datetime(text, required=True)
    except ValueError as exc:
        raise ValueError(f"{option} must be an ISO date or timestamp") from exc


def _parse_api_datetime(value: Any, *, required: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError("timestamp is required")
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)

def _normalize_book(book: dict) -> dict:
    return {
        "id": str(book.get("id") or ""),
        "slug": str(book.get("slug") or ""),
        "name": str(book.get("name") or ""),
        "description": str(book.get("description") or ""),
        "documentCount": int(book.get("items_count") or 0),
        "updatedAt": book.get("content_updated_at") or book.get("updated_at"),
    }


def _normalize_document_summary(item: dict, book: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "slug": str(item.get("slug") or ""),
        "title": str(item.get("title") or ""),
        "type": str(item.get("type") or ""),
        "format": str(item.get("format") or ""),
        "book": {"id": book["id"], "slug": book["slug"], "name": book["name"],
                 "publicAreaLogin": book.get("publicAreaLogin")},
        "author": _normalize_person(item.get("user")),
        "lastEditor": _normalize_person(item.get("last_editor")),
        "wordCount": item.get("word_count"),
        "commentCount": item.get("comments_count"),
        "likeCount": item.get("likes_count"),
        "createdAt": item.get("created_at"),
        "publishedAt": item.get("published_at"),
        "updatedAt": item.get("content_updated_at") or item.get("updated_at"),
    }


def _normalize_search_hit(item: dict, book: dict) -> dict:
    return {
        "id": str(item.get("id") or ""),
        "slug": str(item.get("slug") or ""),
        "title": str(item.get("title") or ""),
        "type": str(item.get("type") or ""),
        "book": {
            "id": book["id"],
            "slug": book["slug"],
            "name": str(item.get("book_name") or book["name"]),
        },
        "url": str(item.get("url") or ""),
        "snippet": None,
    }

def _normalize_person(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    name = str(
        value.get("publicName") or value.get("name") or value.get("login") or ""
    ).strip()
    if not name:
        return None
    return {
        "id": str(value.get("id") or ""),
        "login": str(value.get("login") or ""),
        "name": name,
    }


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("numeric option is invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"numeric option must be between {minimum} and {maximum}")
    return parsed
