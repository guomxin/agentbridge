from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.yuque import (
    YUQUE_DOCUMENT_CATALOG_CAPABILITY,
    YUQUE_DOCUMENT_READ_CAPABILITY,
    YUQUE_DOCUMENT_SEARCH_CAPABILITY,
    YUQUE_PUBLIC_BOOKS_CAPABILITY,
    YuqueCentralAdapter,
    YuqueLoginRequired,
    build_yuque_capability_registry,
    lake_to_plain_text,
    redact_sensitive_text,
)


class YuqueAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = YuqueCentralAdapter(
            base_url="https://tc-aiot.yuque.com",
            organization_id=20020375,
        )

    def test_authentication_contract_requires_trusted_interactive_browser(self):
        contract = self.adapter.authentication_contract()

        self.assertEqual(contract["system_id"], "yuque")
        self.assertEqual(contract["authentication_mode"], "interactive_browser")
        self.assertEqual(contract["fields"], [])
        self.assertTrue(contract["interactive"]["requires_human_verification"])
        self.assertIn("/login?", contract["interactive"]["entry_url"])

    def test_probe_session_verifies_current_principal(self):
        worker = FakeYuqueWorker()

        result = self.adapter.probe_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(result["observed_principal_ref"], "辛国茂")
        self.assertEqual(result["transport"], "central_browser_cookie")

    def test_probe_session_rejects_redirect_to_login(self):
        worker = FakeYuqueWorker(login_required=True)

        with self.assertRaises(YuqueLoginRequired):
            self.adapter.probe_session(worker)

    def test_lists_public_books_and_document_catalog(self):
        worker = FakeYuqueWorker()

        books = self.adapter.invoke_capability(
            YUQUE_PUBLIC_BOOKS_CAPABILITY,
            worker,
            {},
        )
        catalog = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_CATALOG_CAPABILITY,
            worker,
            {"book": "共享文档", "keyword": "设备", "limit": 20},
        )

        self.assertEqual(books["publicArea"]["login"], "org-wiki-tc-aiot-ms6e4o")
        self.assertEqual([item["name"] for item in books["items"]], ["共享文档"])
        self.assertEqual(catalog["count"], 1)
        self.assertEqual(catalog["items"][0]["title"], "对接设备清单")
        self.assertNotIn("description", catalog["items"][0])
        self.assertNotIn("catalog-secret", str(catalog))

    def test_search_omits_server_snippets(self):
        worker = FakeYuqueWorker()

        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_SEARCH_CAPABILITY,
            worker,
            {"book": "共享文档", "query": "物联网平台", "limit": 10},
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["snippet"], None)
        self.assertNotIn("super-secret", str(result))
        query = parse_qs(urlparse(worker.urls[-1]).query)
        self.assertEqual(query["scope"], ["org-wiki-tc-aiot-ms6e4o/tabv3n"])

    def test_read_converts_lake_and_redacts_likely_secrets(self):
        worker = FakeYuqueWorker()

        result = self.adapter.invoke_capability(
            YUQUE_DOCUMENT_READ_CAPABILITY,
            worker,
            {"book": "共享文档", "document": "对接设备清单", "max_chars": 5000},
        )

        self.assertIn("设备清单", result["content"])
        self.assertIn("密码：[REDACTED]", result["content"])
        self.assertIn("token: [REDACTED]", result["content"])
        self.assertNotIn("super-secret", result["content"])
        self.assertTrue(result["redaction"]["applied"])
        self.assertEqual(
            set(result["redaction"]["categories"]),
            {"credential", "token"},
        )

    def test_lake_text_and_redaction_helpers_are_conservative(self):
        text = lake_to_plain_text(
            "<!doctype lake><p>第一行<br>第二行</p><script>bad()</script>"
        )
        redacted, categories = redact_sensitive_text(
            "用户名：admin\nhttps://alice:secret@example.test/path"
        )

        self.assertEqual(text, "第一行\n第二行")
        self.assertNotIn("admin", redacted)
        self.assertIn("https://[REDACTED]@example.test/path", redacted)
        self.assertEqual(set(categories), {"credential", "url_credential"})

    def test_registry_contains_only_read_capabilities(self):
        capabilities = build_yuque_capability_registry().list()

        self.assertEqual(len(capabilities), 4)
        self.assertTrue(all(spec.effect == "read" for spec in capabilities))
        self.assertEqual(
            {spec.name for spec in capabilities},
            {
                YUQUE_PUBLIC_BOOKS_CAPABILITY,
                YUQUE_DOCUMENT_CATALOG_CAPABILITY,
                YUQUE_DOCUMENT_SEARCH_CAPABILITY,
                YUQUE_DOCUMENT_READ_CAPABILITY,
            },
        )


class FakeYuqueWorker:
    def __init__(self, *, login_required: bool = False) -> None:
        self.login_required = login_required
        self.urls: list[str] = []
        self.page_url = ""

    def goto(self, url: str, *, timeout_seconds: float = 30):
        del timeout_seconds
        self.page_url = url
        return self

    def request(self, method: str, url: str, **_kwargs) -> dict:
        self.assert_method(method)
        self.urls.append(url)
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self.login_required or path == "/login":
            return _response(
                status=302,
                url="https://tc-aiot.yuque.com/login?org=tc-aiot",
                payload=None,
            )
        if path == "/api/mine":
            return _response(
                payload={
                    "data": {
                        "id": 38984710,
                        "login": "xinguomao",
                        "name": "辛国茂",
                        "isInCurrentOrg": True,
                    }
                }
            )
        if path == "/api/modules/org_wiki/wiki/show":
            if query.get("organizationId") != ["20020375"]:
                raise AssertionError(f"unexpected organization query: {query}")
            return _response(payload=_public_area_payload())
        if path == "/api/docs" and query.get("book_id") == ["41980403"]:
            return _response(
                payload={
                    "data": [
                        {
                            "id": 262116028,
                            "slug": "xc22kk0yg6ovnaht",
                            "title": "对接设备清单",
                            "type": "Doc",
                            "description": "密码：catalog-secret",
                            "word_count": 100,
                            "updated_at": "2026-07-28T00:00:00Z",
                        },
                        {
                            "id": 201596874,
                            "slug": "ankn1v3zqsqcfn5y",
                            "title": "黄佳豪工作日报+周报",
                            "type": "Sheet",
                        },
                    ]
                }
            )
        if path == "/api/zsearch":
            return _response(
                payload={
                    "data": {
                        "hits": [
                            {
                                "id": 262116028,
                                "slug": "xc22kk0yg6ovnaht",
                                "title": "对接设备清单",
                                "type": "Doc",
                                "book_name": "共享文档",
                                "url": "/org-wiki-tc-aiot-ms6e4o/tabv3n/xc22kk0yg6ovnaht",
                                "abstract": "密码：super-secret",
                            }
                        ],
                        "totalHits": 1,
                    }
                }
            )
        if path == "/api/docs/xc22kk0yg6ovnaht":
            return _response(
                payload={
                    "data": {
                        "id": 262116028,
                        "slug": "xc22kk0yg6ovnaht",
                        "title": "对接设备清单",
                        "type": "Doc",
                        "content": (
                            "<!doctype lake><h1>设备清单</h1>"
                            "<p>密码：super-secret</p>"
                            "<p>token: abcdef123456</p>"
                        ),
                        "word_count": 6,
                        "user": {"id": 1, "login": "author", "name": "作者"},
                        "contributors": [],
                    }
                }
            )
        raise AssertionError(f"unexpected Yuque URL: {url}")

    @staticmethod
    def assert_method(method: str) -> None:
        if method != "GET":
            raise AssertionError(f"unexpected method: {method}")


def _public_area_payload() -> dict:
    return {
        "wiki": {
            "id": 38984659,
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
                                        "name": "知识库分组",
                                        "books": [
                                            {
                                                "id": 41980403,
                                                "slug": "tabv3n",
                                                "name": "共享文档",
                                                "items_count": 138,
                                            }
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


def _response(*, payload, status: int = 200, url: str = "https://tc-aiot.yuque.com/api") -> dict:
    return {
        "status": status,
        "url": url,
        "content_type": "application/json",
        "json": payload,
        "text": "",
        "elapsed_ms": 1,
    }


if __name__ == "__main__":
    unittest.main()
