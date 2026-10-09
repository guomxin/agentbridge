from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlparse
import unittest
import zlib

from agentbridge.adapters.yuque import (
    YUQUE_DOCUMENT_CATALOG_CAPABILITY,
    YUQUE_DOCUMENT_READ_CAPABILITY,
    YUQUE_DOCUMENT_SEARCH_CAPABILITY,
    YuqueCentralAdapter,
    lake_to_structured_text,
    redact_sensitive_text,
)


class YuqueReadExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = YuqueCentralAdapter(
            base_url="https://tc-aiot.yuque.com",
            organization_id=20020375,
        )

    def test_catalog_aggregates_filters_sorts_and_pages_all_books(self) -> None:
        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_CATALOG_CAPABILITY,
            ExtendedYuqueWorker(),
            {
                "document_type": "doc",
                "updated_after": "2026-01-01",
                "sort": "updated_asc",
                "page": 1,
                "limit": 1,
            },
        )

        self.assertEqual(result["scope"], "all_books")
        self.assertEqual(result["booksScanned"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["hasMore"])
        self.assertEqual(result["items"][0]["title"], "设备说明")
        self.assertEqual(result["items"][0]["book"]["name"], "共享文档")
        self.assertEqual(result["items"][0]["format"], "lake")

    def test_global_search_uses_organization_scope_and_resolves_hit_book(self) -> None:
        worker = ExtendedYuqueWorker()

        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_SEARCH_CAPABILITY,
            worker,
            {"query": "会议", "limit": 20},
        )

        query = parse_qs(urlparse(worker.urls[-1]).query)
        self.assertEqual(query["scope"], ["org-wiki-tc-aiot-ms6e4o"])
        self.assertEqual(query["tab"], ["book"])
        self.assertEqual(result["scope"], "all_books")
        self.assertEqual(result["booksSearched"], 2)
        self.assertEqual(result["items"][0]["book"]["name"], "会议记录")
        self.assertIsNone(result["items"][0]["snippet"])
        self.assertNotIn("incidental-secret", str(result))

    def test_lake_parser_preserves_outline_table_image_ocr_and_redaction(self) -> None:
        card = quote(
            "data:"
            + json.dumps(
                {
                    "title": "部署图",
                    "width": 800,
                    "height": 600,
                    "originalType": "png",
                    "ocrLocations": [{"text": "密码：image-secret"}],
                },
                ensure_ascii=False,
            ),
            safe="",
        )
        text, structure = lake_to_structured_text(
            "<!doctype lake><h1>方案</h1>"
            "<table><tr><th>设备</th><th>状态</th></tr>"
            "<tr><td>网关</td><td>正常</td></tr></table>"
            f'<card type="inline" name="image" value="{card}"></card>'
        )
        sanitized, categories = redact_sensitive_text(text)

        self.assertIn("# 方案", text)
        self.assertIn("| 设备 | 状态 |", text)
        self.assertEqual(structure["outline"], [{"level": 1, "text": "方案"}])
        self.assertEqual(structure["tables"][0]["rowCount"], 2)
        self.assertEqual(structure["images"][0]["width"], 800)
        self.assertIn("密码：[REDACTED]", sanitized)
        self.assertIn("credential", categories)

    def test_sheet_read_decompresses_cells_and_supports_row_paging(self) -> None:
        worker = ExtendedYuqueWorker()

        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_READ_CAPABILITY,
            worker,
            {
                "document": "团队日志",
                "row_offset": 1,
                "max_rows": 1,
                "max_chars": 5000,
            },
        )

        self.assertEqual(result["contentFormat"], "tabular_text_from_lakesheet")
        self.assertIn("| A | B |", result["content"])
        self.assertIn("| 2026-07-29 | 完成适配 |", result["content"])
        self.assertNotIn("日期", result["content"])
        self.assertEqual(result["structure"]["sheets"][0]["returnedRows"], 1)
        self.assertTrue(result["structure"]["sheets"][0]["hasMore"])

    def test_data_table_read_fetches_rows_from_read_only_show_endpoint(self) -> None:
        worker = ExtendedYuqueWorker()

        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_READ_CAPABILITY,
            worker,
            {
                "document": "测试记录",
                "max_rows": 20,
                "max_chars": 5000,
            },
        )

        self.assertEqual(result["contentFormat"], "tabular_text_from_laketable")
        self.assertIn("| 状态 | 日期 |", result["content"])
        self.assertIn("| 完成 | 2026-07-29 |", result["content"])
        self.assertEqual(result["structure"]["sheets"][0]["returnedRows"], 1)
        show_url = next(
            value for value in worker.urls if "TableRecordController/show" in value
        )
        query = parse_qs(urlparse(show_url).query)
        self.assertEqual(query["limit"], ["20"])
        self.assertEqual(query["offset"], ["0"])


class ExtendedYuqueWorker:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs) -> dict:
        if method != "GET":
            raise AssertionError(f"unexpected method: {method}")
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/modules/org_wiki/wiki/show":
            return _response(_public_area_payload())
        if parsed.path == "/api/docs":
            book_id = query.get("book_id", [""])[0]
            return _response({"data": _catalog(book_id)})
        if parsed.path == "/api/zsearch":
            return _response(
                {
                    "data": {
                        "hits": [
                            {
                                "id": 31,
                                "slug": "meeting-note",
                                "title": "会议纪要",
                                "type": "Doc",
                                "book_name": "会议记录",
                                "url": (
                                    "/org-wiki-tc-aiot-ms6e4o/"
                                    "meetings/meeting-note"
                                ),
                                "abstract": "密码：incidental-secret",
                            }
                        ],
                        "totalHits": 1,
                    }
                }
            )
        if parsed.path == "/api/docs/team-sheet":
            return _response({"data": _sheet_document()})
        if parsed.path == "/api/docs/test-table":
            return _response({"data": _table_document()})
        if parsed.path == "/api/docs/device-doc":
            return _response(
                {
                    "data": {
                        **_catalog("book-a")[0],
                        "content": "<!doctype lake><p>设备正文</p>",
                        "contributors": [],
                    }
                }
            )
        if parsed.path.endswith("/TableRecordController/show"):
            return _response(
                {
                    "records": [
                        {
                            "data": json.dumps(
                                {
                                    "status-column": {"value": "完成"},
                                    "date-column": {
                                        "value": {
                                            "text": "2026-07-29",
                                            "time": "2026-07-29T00:00:00Z",
                                        }
                                    },
                                },
                                ensure_ascii=False,
                            )
                        }
                    ],
                    "hasMore": False,
                    "users": [],
                }
            )
        raise AssertionError(f"unexpected Yuque URL: {url}")


def _catalog(book_id: str) -> list[dict]:
    if book_id == "book-a":
        return [
            {
                "id": 11,
                "slug": "device-doc",
                "title": "设备说明",
                "type": "Doc",
                "format": "lake",
                "created_at": "2026-01-01T00:00:00Z",
                "content_updated_at": "2026-02-01T00:00:00Z",
                "word_count": 10,
            },
            {
                "id": 12,
                "slug": "team-sheet",
                "title": "团队日志",
                "type": "Sheet",
                "format": "lakesheet",
                "content_updated_at": "2026-03-01T00:00:00Z",
                "word_count": 0,
            },
            {
                "id": 13,
                "slug": "test-table",
                "title": "测试记录",
                "type": "Table",
                "format": "laketable",
                "content_updated_at": "2026-04-01T00:00:00Z",
                "word_count": 0,
            },
        ]
    if book_id == "book-b":
        return [
            {
                "id": 31,
                "slug": "meeting-note",
                "title": "会议纪要",
                "type": "Doc",
                "format": "lake",
                "content_updated_at": "2026-05-01T00:00:00Z",
                "word_count": 20,
            }
        ]
    return []


def _sheet_document() -> dict:
    sheet = [
        {
            "name": "日志",
            "data": {
                "0": {"0": {"v": "日期"}, "1": {"v": "内容"}},
                "1": {"0": {"v": "2026-07-29"}, "1": {"v": "完成适配"}},
                "2": {"0": {"v": "2026-07-30"}, "1": {"v": "继续验证"}},
            },
        }
    ]
    compressed = zlib.compress(
        json.dumps(sheet, ensure_ascii=False).encode("utf-8")
    ).decode("latin1")
    return {
        **_catalog("book-a")[1],
        "content": json.dumps(
            {"format": "lakesheet", "sheet": compressed},
            ensure_ascii=False,
        ),
        "contributors": [],
    }


def _table_document() -> dict:
    return {
        **_catalog("book-a")[2],
        "content": json.dumps(
            {
                "format": "laketable",
                "sheetId": "table-sheet-id",
                "sheet": [
                    {
                        "name": "测试记录",
                        "columns": [
                            {"id": "status-column", "name": "状态"},
                            {"id": "date-column", "name": "日期"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "contributors": [],
    }


def _public_area_payload() -> dict:
    return {
        "wiki": {
            "id": 1,
            "name": "公共区",
            "login": "org-wiki-tc-aiot-ms6e4o",
        },
        "layouts": [
            {
                "placements": [
                    {
                        "blocks": [
                            {
                                "type": "bookStacks",
                                "data": [
                                    {
                                        "name": "部门知识库",
                                        "books": [
                                            {
                                                "id": "book-a",
                                                "slug": "shared",
                                                "name": "共享文档",
                                                "items_count": 3,
                                            },
                                            {
                                                "id": "book-b",
                                                "slug": "meetings",
                                                "name": "会议记录",
                                                "items_count": 1,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ],
    }


def _response(payload: dict) -> dict:
    return {
        "status": 200,
        "url": "https://tc-aiot.yuque.com/api",
        "content_type": "application/json",
        "json": payload,
        "text": json.dumps(payload, ensure_ascii=False),
    }
