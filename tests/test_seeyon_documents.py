from __future__ import annotations

import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_documents import (
    SeeyonDocumentAccessDenied,
    _certificate_content_type,
    _certificate_search_queries,
    _certificate_title,
    _request_download_with_redirects,
    _validated_queries,
    _validated_reference,
    fetch_certificate_documents,
    search_certificate_documents,
)


class SeeyonCertificateSearchTests(unittest.TestCase):
    def test_search_ranks_title_match_and_hides_inaccessible_rows(self):
        patent_rows = [
            _row(
                resource_id="patent-1",
                filename=(
                    "\u3010TH-26-1-16\u3011\u3010SDZM-26-1-06\u3011"
                    "\u4e00\u79cd\u57fa\u4e8e\u52a8\u6001\u6e29\u5dee\u8865\u507f\u7684"
                    "\u5149\u654f\u503c\u4fee\u6b63\u65b9\u6cd5\u53ca\u7cfb\u7edf.pdf"
                ),
            ),
            _row(
                resource_id="patent-2",
                filename="\u5176\u4ed6\u5149\u654f\u503c\u4fee\u6b63\u65b9\u6cd5.pdf",
            ),
            _row(
                resource_id="patent-3",
                filename="\u4e0d\u53ef\u4e0b\u8f7d\u7684\u5149\u654f\u503c\u6587\u6863.pdf",
                download_acl=False,
            ),
        ]
        copyright_rows = []
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ),
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                side_effect=[patent_rows, copyright_rows],
            ),
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "name": (
                        "\u4e00\u79cd\u57fa\u4e8e\u52a8\u6001\u6e29\u5dee\u8865\u507f\u7684"
                        "\u5149\u654f\u503c\u4fee\u6b63\u65b9\u6cd5\u53ca\u7cfb\u7edf"
                    ),
                    "document_type": "all",
                    "limit": 10,
                },
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["match_kind"], "exact")
        self.assertEqual(
            result["items"][0]["title"],
            "\u4e00\u79cd\u57fa\u4e8e\u52a8\u6001\u6e29\u5dee\u8865\u507f\u7684"
            "\u5149\u654f\u503c\u4fee\u6b63\u65b9\u6cd5\u53ca\u7cfb\u7edf",
        )
        self.assertIn("_download_reference", result["items"][0])
        self.assertEqual(result["inaccessible_count"], 0)

    def test_search_rejects_short_query_and_unsupported_type(self):
        with self.assertRaisesRegex(ValueError, "at least 2"):
            search_certificate_documents(
                object(),
                base_url="http://oa.example.test",
                arguments={"name": "a"},
            )
        with self.assertRaisesRegex(ValueError, "document_type"):
            search_certificate_documents(
                object(),
                base_url="http://oa.example.test",
                arguments={"name": "certificate", "document_type": "other"},
            )

    def test_batch_search_strips_software_versions_then_checks_candidates(self):
        first_rows = [
            _row(resource_id="soft-wrong", filename="系统甲V2.0.jpg"),
            _row(resource_id="soft-1", filename="系统甲V1.0.jpg"),
        ]
        second_rows = [_row(resource_id="soft-2", filename="系统乙V1.0.pdf")]
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ) as open_category,
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                side_effect=[first_rows, second_rows],
            ) as search_folder,
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "names": ["系统甲V1.0", "系统乙Ｖ 1.0"],
                    "document_type": "software_copyright_certificate",
                    "limit": 10,
                },
            )

        self.assertEqual(open_category.call_count, 1)
        self.assertEqual(
            [call.kwargs["query"] for call in search_folder.call_args_list],
            ["系统甲", "系统乙"],
        )
        self.assertEqual(result["schema_version"], "agentbridge.oa_certificate_search.v2")
        self.assertEqual(result["queries"], ["系统甲V1.0", "系统乙Ｖ 1.0"])
        self.assertEqual(result["matched_queries"], result["queries"])
        self.assertEqual(result["unmatched_queries"], [])
        self.assertEqual(
            [(item["query"], item["title"]) for item in result["items"]],
            [("系统甲V1.0", "系统甲V1.0"), ("系统乙Ｖ 1.0", "系统乙V1.0")],
        )

    def test_structured_software_request_preserves_version_and_aliases(self):
        rows = [
            _row(resource_id="soft-v1", filename="泰华视图云大数据平台软件V1.0.jpg"),
            _row(resource_id="soft-v2", filename="泰华视图云大数据平台软件V2.0.jpg"),
        ]
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ),
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                return_value=rows,
            ) as search_folder,
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "documents": [
                        {
                            "name": "泰华视图云大数据平台软件",
                            "version": "V2.0",
                            "aliases": ["视图云大数据平台"],
                        }
                    ],
                    "document_type": "software_copyright_certificate",
                },
            )

        self.assertEqual(search_folder.call_args_list[0].kwargs["query"], "泰华视图云大数据平台软件")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["ambiguous_queries"], [])
        self.assertEqual(result["items"][0]["title"], "泰华视图云大数据平台软件V2.0")
        self.assertEqual(result["items"][0]["requested_version"], "v2.0")
        self.assertEqual(
            result["items"][0]["_download_reference"]["requested_version"],
            "v2.0",
        )

    def test_versionless_software_request_reports_ambiguity_without_download(self):
        rows = [
            _row(resource_id="soft-v1", filename="泰华视图云大数据平台软件V1.0.jpg"),
            _row(resource_id="soft-v2", filename="泰华视图云大数据平台软件V2.0.jpg"),
        ]
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ),
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                return_value=rows,
            ),
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "documents": [{"name": "泰华视图云大数据平台软件"}],
                    "document_type": "software_copyright_certificate",
                },
            )

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["matched_queries"], [])
        self.assertEqual(result["unmatched_queries"], [])
        self.assertEqual(
            result["ambiguous_queries"],
            ["泰华视图云大数据平台软件"],
        )
        self.assertEqual(result["ambiguities"][0]["versions"], ["V1.0", "V2.0"])

    def test_software_search_uses_formal_name_then_bracketed_short_name(self):
        query = "泰华视图云大数据平台软件[简称:视图云大数据平台]V2.0"
        self.assertEqual(
            _certificate_search_queries(
                query,
                "software_copyright_certificate",
            ),
            ("泰华视图云大数据平台软件", "视图云大数据平台"),
        )
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ) as open_category,
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                side_effect=[
                    [],
                    [
                        _row(
                            resource_id="soft-short-name",
                            filename="视图云大数据平台V2.0.jpg",
                        )
                    ],
                ],
            ) as search_folder,
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "name": query,
                    "document_type": "software_copyright_certificate",
                },
            )

        self.assertEqual(open_category.call_count, 1)
        self.assertEqual(
            [call.kwargs["query"] for call in search_folder.call_args_list],
            ["泰华视图云大数据平台软件", "视图云大数据平台"],
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matched_queries"], [query])
        self.assertEqual(result["items"][0]["title"], "视图云大数据平台V2.0")

    def test_software_search_does_not_treat_plain_bracket_notes_as_aliases(self):
        self.assertEqual(
            _certificate_search_queries(
                "泰华数据平台[企业版]V2.0",
                "software_copyright_certificate",
            ),
            ("泰华数据平台",),
        )

    def test_batch_fetch_opens_each_certificate_category_only_once(self):
        first = {
            **_row(resource_id="soft-1", filename="系统甲V1.0.jpg"),
            "document_type": "software_copyright_certificate",
            "category_label": "2-著作权证书扫描件",
        }
        second = {
            **_row(resource_id="soft-2", filename="系统乙V1.0.pdf"),
            "document_type": "software_copyright_certificate",
            "category_label": "2-著作权证书扫描件",
        }
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ) as open_category,
            patch(
                "agentbridge.adapters.seeyon_documents._fetch_certificate_document_from_frame",
                side_effect=[{"filename": first["filename"]}, {"filename": second["filename"]}],
            ) as fetch_one,
        ):
            result = fetch_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                references=[first, second],
            )

        self.assertEqual(open_category.call_count, 1)
        self.assertEqual(fetch_one.call_count, 2)
        self.assertEqual(
            [item["filename"] for item in result],
            ["系统甲V1.0.jpg", "系统乙V1.0.pdf"],
        )

    def test_batch_reports_unmatched_queries_and_keeps_one_slot_per_name(self):
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ),
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                side_effect=[
                    [
                        _row(resource_id="patent-1", filename="专利甲.pdf"),
                        _row(resource_id="patent-2", filename="专利甲附录.pdf"),
                    ],
                    [_row(resource_id="patent-3", filename="专利乙.pdf")],
                    [],
                ],
            ),
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "names": ["专利甲", "专利乙", "专利丙"],
                    "document_type": "patent_certificate",
                    "limit": 1,
                },
            )

        self.assertEqual(result["matched_queries"], ["专利甲", "专利乙"])
        self.assertEqual(result["unmatched_queries"], ["专利丙"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(
            [item["query"] for item in result["items"]],
            ["专利甲", "专利乙"],
        )

    def test_patent_search_preserves_trailing_version_like_text(self):
        with (
            patch(
                "agentbridge.adapters.seeyon_documents._open_certificate_category",
                return_value=object(),
            ),
            patch(
                "agentbridge.adapters.seeyon_documents._search_current_folder",
                return_value=[_row(resource_id="patent-1", filename="专利V1.0.pdf")],
            ) as search_folder,
        ):
            result = search_certificate_documents(
                object(),
                base_url="http://oa.example.test/seeyon/main.do",
                arguments={
                    "name": "专利V1.0",
                    "document_type": "patent_certificate",
                },
            )

        self.assertEqual(search_folder.call_args.kwargs["query"], "专利V1.0")
        self.assertEqual(result["count"], 1)

    def test_batch_accepts_twenty_names_and_rejects_twenty_one(self):
        names = [f"专利名称{index:02d}" for index in range(20)]
        self.assertEqual(_validated_queries({"names": names}), names)
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            _validated_queries({"names": names + ["专利名称20"]})

    def test_search_requires_name_or_names(self):
        with self.assertRaisesRegex(ValueError, "name or names"):
            search_certificate_documents(
                object(),
                base_url="http://oa.example.test",
                arguments={},
            )

    def test_reference_rejects_category_substitution(self):
        reference = {
            **_row(resource_id="patent-1", filename="certificate.pdf"),
            "document_type": "patent_certificate",
            "category_label": "2-\u8457\u4f5c\u6743\u8bc1\u4e66\u626b\u63cf\u4ef6",
        }

        with self.assertRaises(SeeyonDocumentAccessDenied):
            _validated_reference(reference)

    def test_reference_rejects_requested_software_version_mismatch(self):
        reference = {
            **_row(resource_id="soft-1", filename="系统甲V1.0.jpg"),
            "document_type": "software_copyright_certificate",
            "category_label": "2-著作权证书扫描件",
            "requested_version": "V2.0",
        }
        with self.assertRaisesRegex(
            SeeyonDocumentAccessDenied,
            "requested software version",
        ):
            _validated_reference(reference)

    def test_reference_accepts_image_scan_and_rejects_other_extensions(self):
        image_reference = {
            **_row(resource_id="soft-1", filename="certificate.jpg"),
            "document_type": "software_copyright_certificate",
            "category_label": "2-著作权证书扫描件",
        }
        self.assertEqual(
            _validated_reference(image_reference)["filename"],
            "certificate.jpg",
        )

        image_reference["filename"] = "certificate.zip"
        with self.assertRaises(SeeyonDocumentAccessDenied):
            _validated_reference(image_reference)

    def test_certificate_title_removes_internal_codes_but_preserves_real_title(self):
        self.assertEqual(
            _certificate_title(
                "\u200b&\u3010TH-26-1-16 \u3011\u3010SDZM-26-1-06\u3011"
                "\u667a\u80fd\u5171\u7a7a\u76d1\u6d4b\u7cfb\u7edf.pdf"
            ),
            "\u667a\u80fd\u5171\u7a7a\u76d1\u6d4b\u7cfb\u7edf",
        )


    def test_certificate_content_type_requires_matching_extension_and_magic(self):
        self.assertEqual(
            _certificate_content_type("scan.jpg", b"\xff\xd8\xff\xe0data"),
            "image/jpeg",
        )
        self.assertEqual(
            _certificate_content_type("scan.png", b"\x89PNG\r\n\x1a\ndata"),
            "image/png",
        )
        self.assertIsNone(
            _certificate_content_type("scan.jpg", b"%PDF-1.7")
        )
        self.assertIsNone(
            _certificate_content_type("scan.zip", b"PK\x03\x04")
        )

    def test_download_follows_only_worker_validated_redirects(self):
        worker = RedirectWorker(
            [
                {
                    "status": 302,
                    "url": "http://oa.example.test/seeyon/fileDownload.do",
                    "location": "/seeyon/files/certificate.pdf",
                },
                {
                    "status": 200,
                    "url": "http://oa.example.test/seeyon/files/certificate.pdf",
                    "body": b"%PDF-1.7",
                },
            ]
        )

        response = _request_download_with_redirects(
            worker,
            "http://oa.example.test/seeyon/fileDownload.do",
        )

        self.assertEqual(response["status"], 200)
        self.assertEqual(
            worker.urls,
            [
                "http://oa.example.test/seeyon/fileDownload.do",
                "http://oa.example.test/seeyon/files/certificate.pdf",
            ],
        )

    def test_download_rejects_redirect_without_location(self):
        worker = RedirectWorker(
            [{"status": 302, "url": "http://oa.example.test/download"}]
        )

        with self.assertRaisesRegex(Exception, "no location"):
            _request_download_with_redirects(
                worker,
                "http://oa.example.test/download",
            )
def _row(
    *,
    resource_id: str,
    filename: str,
    download_acl: bool = True,
) -> dict:
    return {
        "resource_id": resource_id,
        "source_id": f"source-{resource_id}",
        "filename": filename,
        "display_size": "1 MB",
        "create_date": "2026-07-27",
        "version": "v1",
        "mime_type_id": "22",
        "secret_level": "1",
        "read_acl": True,
        "download_acl": download_acl,
        "is_upload_file": True,
    }


class RedirectWorker:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def request_bytes(self, _method, url, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)
if __name__ == "__main__":
    unittest.main()
