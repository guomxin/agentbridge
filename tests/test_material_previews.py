from base64 import b64decode
from copy import deepcopy
from io import BytesIO
import http.client
import json
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

from agentbridge.adapters.seeyon_material_preview import (
    MaterialPreviewError, _canonical_reference, _archive, fetch_current_material,
    material_references, previews_for_review, public_manifest,
)
from agentbridge.auth.business_draft_card import BusinessDraftApplication
from agentbridge.auth.material_preview import BusinessMaterialPreviewApplication, material_preview_target
from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.material_previews import BusinessMaterialPreviewService
from tests.authorization_fixtures import grant_permissions
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, SyntheticOaWorker, OWNER


BASE = "https://oa.example.test/seeyon/"
IMAGE = "/seeyon/fileUpload.do?method=showRTE&type=image&fileId=123&createDate=2026-10-22"
ZIP = "/seeyon/rest/attachment/file/batchDownload/123,-456?zipFileName=attachments.zip"
PDF = "/seeyon/fileDownload.do?method=download&viewMode=download&fileId=123&createDate=2026-10-22&filename=example.pdf&v=1"
PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1EAAAAASUVORK5CYII=")


def zip_bytes(entries, *, compression=ZIP_DEFLATED):
    out = BytesIO()
    with ZipFile(out, "w", compression=compression) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return out.getvalue()


class MaterialReferenceTests(unittest.TestCase):
    def test_observed_rte_and_archive_routes_have_opaque_manifest_without_urls(self):
        refs = material_references(text=f'<p>材料<img src="{IMAGE}" alt="图示"></p>',
            attachments=[{"name": "附件包", "href": BASE.rstrip("/")[:-7] + ZIP}], base_url=BASE)
        self.assertEqual([item["public"]["kind"] for item in refs], ["image", "archive"])
        self.assertTrue(all(item["public"]["state"] == "available" for item in refs))
        encoded = json.dumps(public_manifest(refs))
        for private in ("showRTE", "fileId", "createDate", "https:", "batchDownload"):
            self.assertNotIn(private, encoded)
        self.assertEqual(refs, material_references(text=f'<p>材料<img src="{IMAGE}" alt="图示"></p>',
            attachments=[{"name": "附件包", "href": "https://oa.example.test" + ZIP}], base_url=BASE))

    def test_closed_url_contract_rejects_cross_origin_mutations_tokens_and_redirect_forms(self):
        bad = ["https://evil.test" + IMAGE, "//oa.example.test" + IMAGE, "javascript:" + IMAGE,
               IMAGE + "&fileId=999", IMAGE + "&token=private", IMAGE + "#private",
               IMAGE.replace("showRTE", "delete"), IMAGE.replace("2026-10-22", "2026-99-22"),
               IMAGE.replace("123", "../123"), IMAGE.replace("/seeyon/", "/seeyon/../"),
               "/seeyon/fileUpload.do?method=download&fileId=123", "https://user:secret@oa.example.test" + IMAGE]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(_canonical_reference(value, base_url=BASE))

    def test_legacy_projection_does_not_mutate_original_or_claim_names_only_are_readable(self):
        review = {"sections": [{"kind": "text", "text": f'<p>旧卡<img src="{IMAGE}" alt="说明"></p>'}],
                  "attachments": [{"name": "只有名称.pdf"}]}
        old = deepcopy(review)
        result = previews_for_review(review)
        self.assertEqual(review, old)
        self.assertEqual([item["state"] for item in result], ["available", "unavailable"])
        self.assertEqual(previews_for_review({**review, "materialPreviews": result}), result)

    def test_parser_does_not_make_hidden_scripts_or_external_images_executable(self):
        refs = material_references(text=f'<script><img src="{IMAGE}"></script><div hidden><img src="{IMAGE}"></div><img src="https://external.test/picture.png">')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["public"]["state"], "unavailable")
        self.assertEqual(refs[0]["route"], "")

    def test_pdf_route_is_explicit_and_png_signature_does_not_relax_pdf_contract(self):
        self.assertEqual(_canonical_reference(PDF, base_url=BASE)[0], "pdf")
        self.assertIsNone(_canonical_reference(PDF.replace("example.pdf", "example.html"), base_url=BASE))

    def test_material_http_path_is_exact_and_does_not_accept_token_query(self):
        source, presentation, material = "a" * 32, "b" * 32, "c" * 64
        path = f"/flow/input/{source}/present/{presentation}/draft/materials/{material}"
        self.assertEqual(material_preview_target(path), (source, presentation, material))
        self.assertIsNone(material_preview_target(path + "?token=private"))
        self.assertIsNone(material_preview_target(path + "/anything"))


class ArchivePreviewTests(unittest.TestCase):
    def test_mixed_archive_retains_unsupported_items_and_original_bytes(self):
        result = _archive(zip_bytes([("图.png", PNG), ("说明.pdf", b"%PDF-1.4\nsynthetic"),
                                    ("sheet.xlsx", b"PK\x03\x04synthetic-office"), ("readme.txt", b"text")]), "附件包")
        self.assertEqual(result["totalEntries"], 4)
        self.assertEqual([item["state"] for item in result["items"]], ["available", "available", "unavailable", "unavailable"])
        self.assertEqual(b64decode(result["items"][0]["bodyBase64"]), PNG)
        self.assertNotIn("bodyBase64", result["items"][2])

    def test_archive_rejects_path_traversal_symlink_nested_and_bomb(self):
        unsafe_names = ["../secret.png", "/secret.png", "C:/secret.png", "..\\secret.png"]
        for name in unsafe_names:
            with self.subTest(name=name), self.assertRaises(MaterialPreviewError):
                _archive(zip_bytes([(name, PNG)]), "包")
        symlink = ZipInfo("link.png"); symlink.create_system = 3; symlink.external_attr = 0o120777 << 16
        with self.assertRaises(MaterialPreviewError):
            _archive(zip_bytes([(symlink, PNG)]), "包")
        for name, data in [("inner.zip", zip_bytes([("picture.png", PNG)])), ("bomb.png", b"x" * 100000)]:
            with self.subTest(name=name), self.assertRaises(MaterialPreviewError):
                _archive(zip_bytes([(name, data)]), "包")

    def test_archive_limits_are_enforced_before_image_delivery(self):
        with self.assertRaises(MaterialPreviewError) as caught:
            _archive(zip_bytes([(f"{i}.png", PNG) for i in range(41)]), "包")
        self.assertEqual(caught.exception.code, "MATERIAL_ARCHIVE_LIMIT")
        with patch("agentbridge.adapters.seeyon_material_preview.MAX_ARCHIVE_EXPANDED", 100):
            with self.assertRaises(MaterialPreviewError):
                _archive(zip_bytes([("1.png", PNG), ("2.png", PNG)]), "包")
        with self.assertRaises(MaterialPreviewError):
            _archive(b"PKinvalid", "包")
        with self.assertRaises(MaterialPreviewError):
            _archive(zip_bytes([("a/picture.png", PNG), ("a/./picture.png", PNG)]), "包")


class MaterialAdapterReadTests(unittest.TestCase):
    def setUp(self):
        self.refs = material_references(text=f'<p><img src="{IMAGE}" alt="图示"></p>', base_url=BASE)
        self.calls = []
        self.response = {"status": 200, "content_type": "image/png", "body": PNG}
        self.adapter = SimpleNamespace(base_url=BASE,
            _fetch_workflow_collection=lambda *_: {"items": [{"affair_id": "1"}]},
            _render_workflow_source_detail=lambda *_: {"_material_preview_sources": self.refs})
        def request(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return {"url": url, **self.response}
        self.worker = SimpleNamespace(request_bytes=request)

    def fetch(self):
        return fetch_current_material(self.adapter, self.worker, affair_id="1", expected=self.refs[0]["public"])

    def test_exact_same_source_get_is_the_only_request(self):
        self.assertEqual(self.fetch(), {"body": PNG, "contentType": "image/png"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "GET")
        self.assertTrue(self.calls[0][1].startswith("https://oa.example.test/seeyon/fileUpload.do?"))

    def test_rte_raster_mime_uses_actual_signature_for_observed_mislabeled_images(self):
        jpeg = b"\xff\xd8\xff\xe0synthetic-jpeg"
        for body, detected_type in ((PNG, "image/png"), (jpeg, "image/jpeg")):
            for declared_type in ("image/png", "image/jpeg", "application/octet-stream"):
                with self.subTest(detected=detected_type, declared=declared_type):
                    self.response.update(body=body, content_type=declared_type)
                    self.assertEqual(self.fetch(), {"body": body, "contentType": detected_type})

    def test_rte_mime_compatibility_rejects_active_unknown_and_mixed_content(self):
        cases = [
            ("image/jpeg", b"<html>not an image</html>"),
            ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
            ("image/jpeg", b"unknown bytes"),
            ("image/png", b"GIF89a"),
            ("image/jpeg", b"%PDF-1.4\nsynthetic"),
            ("text/html", PNG),
            ("image/svg+xml", PNG),
            ("application/pdf", PNG),
            ("image/gif", PNG),
            ("", PNG),
        ]
        for declared_type, body in cases:
            with self.subTest(declared=declared_type, magic=body[:8]):
                self.response.update(body=body, content_type=declared_type)
                with self.assertRaises(MaterialPreviewError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.code, "MATERIAL_TYPE_MISMATCH")

    def test_rte_mime_compatibility_does_not_expand_download_or_pdf_contracts(self):
        cases = [
            (PDF.replace("example.pdf", "example.png"), PNG, "image/jpeg"),
            (PDF.replace("example.pdf", "example.jpg"), b"\xff\xd8\xff\xe0synthetic-jpeg", "image/png"),
            (PDF, b"%PDF-1.4\nsynthetic", "image/jpeg"),
            (PDF, PNG, "image/jpeg"),
        ]
        for route, body, declared_type in cases:
            with self.subTest(route=route, declared=declared_type):
                self.refs = material_references(attachments=[{"name": "材料", "href": route}], base_url=BASE)
                self.assertEqual(self.refs[0]["public"]["state"], "available")
                self.response.update(body=body, content_type=declared_type)
                with self.assertRaises(MaterialPreviewError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.code, "MATERIAL_TYPE_MISMATCH")

    def test_current_item_gone_or_reference_changed_blocks_before_network(self):
        self.adapter._fetch_workflow_collection = lambda *_: {"items": []}
        with self.assertRaises(MaterialPreviewError): self.fetch()
        self.assertEqual(self.calls, [])
        self.adapter._fetch_workflow_collection = lambda *_: {"items": [{"affair_id": "1"}]}
        self.adapter._render_workflow_source_detail = lambda *_: {"_material_preview_sources": []}
        with self.assertRaises(MaterialPreviewError): self.fetch()
        self.assertEqual(self.calls, [])

    def test_redirect_login_html_wrong_origin_mime_or_size_never_become_preview(self):
        cases = [{"status": 302, "location": "https://evil.test/"},
                 {"content_type": "text/html", "body": b"<html>login"},
                 {"url": "https://evil.test/", "body": PNG},
                 {"content_type": "image/png", "body": b"%PDF-1.4"},
                 {"content_type": "application/pdf", "body": PNG}]
        for response in cases:
            with self.subTest(response=response):
                self.response = {"status": 200, "content_type": "image/png", "body": PNG, **response}
                with self.assertRaises(MaterialPreviewError): self.fetch()
        self.response = {"status": 200, "content_type": "image/png", "body": PNG}
        with patch("agentbridge.adapters.seeyon_material_preview.MAX_FILE_BYTES", 10):
            with self.assertRaises(MaterialPreviewError): self.fetch()


class MaterialDraftBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name), seed=False)
        self.addCleanup(self.fixture.close)
        original = self.fixture.oa.add_affair
        def add(*args, **kwargs):
            original(*args, **kwargs)
            self.fixture.oa.data["affairs"][args[0]]["detail"]["fields"].append(
                {"name": "正文", "value": f'<p>合成图示<img src="{IMAGE}" alt="合成图示"></p>'})
            self.fixture.oa.persist()
        with patch.object(self.fixture.oa, "add_affair", add):
            self.case = self.fixture.add_view_case("leave")
        self.drafts = BusinessDraftService(self.fixture.service)
        self.opened = self.drafts.open(self.case["input_id"])
        self.previews = BusinessMaterialPreviewService(self.drafts)
        source = self.fixture.service.field_submissions.get(self.case["input_id"])
        self.material = next(item for item in previews_for_review(source["form_schema"]["review"]) if item["kind"] == "image")
        self.args = {"source_submission_id": self.case["input_id"], "material_id": self.material["id"],
                     "csrf_token": self.opened["csrfToken"], "csrf_cookie": self.opened["csrfToken"]}
        self.reads = []
        def request(_worker, method, url, **kwargs):
            self.reads.append((method, url))
            return {"status": 200, "url": url, "content_type": "image/png", "body": PNG}
        self.patch = patch.object(SyntheticOaWorker, "request_bytes", request, create=True)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def query(self, sql, args=()):
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            return connection.execute(sql, args).fetchall()

    def test_real_draft_auth_and_material_read_leave_business_evidence_unchanged(self):
        sql = "SELECT schema_json,schema_hash,state,submitted_at FROM field_submissions WHERE submission_id=?"
        before = self.query(sql, (self.case["input_id"],))
        draft_before = self.query("SELECT * FROM business_drafts")
        self.assertEqual(self.previews.fetch(**self.args)["body"], PNG)
        self.assertEqual(self.query(sql, (self.case["input_id"],)), before)
        self.assertEqual(self.query("SELECT * FROM business_drafts"), draft_before)
        self.assertEqual(self.query("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(len(self.reads), 1)

    def test_other_viewer_token_and_other_presentation_are_rejected(self):
        with self.assertRaises(BusinessDraftError):
            self.previews.fetch(**{**self.args, "csrf_token": "unrelated-user-token", "csrf_cookie": "unrelated-user-token"})
        presentation = self.fixture.service.field_submissions.create_presentation(
            self.case["input_id"], user_subject=OWNER, endpoint_id="other-endpoint")
        with self.assertRaises(BusinessDraftError):
            self.previews.fetch(**self.args, presentation_id=presentation["presentation_id"])
        self.assertEqual(self.reads, [])

    def test_cross_user_presentation_and_expired_presentation_are_rejected(self):
        presentation = self.fixture.service.field_submissions.create_presentation(
            self.case["input_id"], user_subject=OWNER, endpoint_id="preview-endpoint")
        opened = self.drafts.open(self.case["input_id"], presentation_id=presentation["presentation_id"])
        args = {**self.args, "presentation_id": presentation["presentation_id"],
                "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("UPDATE field_submission_presentations SET user_subject='different-owner' WHERE presentation_id=?",
                               (presentation["presentation_id"],))
        with self.assertRaises(BusinessDraftError): self.previews.fetch(**args)
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("UPDATE field_submission_presentations SET user_subject=?,state='expired' WHERE presentation_id=?",
                               (OWNER, presentation["presentation_id"]))
        with self.assertRaises(BusinessDraftError): self.previews.fetch(**args)
        self.assertEqual(self.reads, [])

    def test_permission_revocation_and_expired_session_prevent_worker_read(self):
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("UPDATE sessions SET state='expired' WHERE system_id='oa'")
        with self.assertRaises(BusinessDraftError): self.previews.fetch(**self.args)
        self.assertEqual(self.reads, [])
        grant_permissions(self.fixture.service.user_grants, OWNER, [])
        with self.assertRaises(BusinessDraftError): self.previews.fetch(**self.args)

    def test_actual_downstream_account_change_is_rejected(self):
        self.fixture.oa.data["principal"] = "other-person"
        with self.assertRaises(BusinessDraftError) as caught: self.previews.fetch(**self.args)
        self.assertEqual(caught.exception.code, "DRAFT_SESSION_CHANGED")
        self.assertEqual(self.reads, [])

    def test_current_material_replacement_is_rejected_before_file_download(self):
        affair = self.fixture.oa.data["affairs"][self.case["arguments"]["affair_id"]]
        affair["detail"]["fields"][-1]["value"] = f'<p><img src="{IMAGE.replace("123", "999")}" alt="合成图示"></p>'
        self.fixture.oa.persist()
        with self.assertRaises(BusinessDraftError) as caught: self.previews.fetch(**self.args)
        self.assertEqual(caught.exception.code, "MATERIAL_SOURCE_CHANGED")
        self.assertEqual(self.reads, [])

    def test_revocation_during_download_discards_bytes(self):
        def revoke(_worker, method, url, **kwargs):
            grant_permissions(self.fixture.service.user_grants, OWNER, [])
            return {"status": 200, "url": url, "content_type": "image/png", "body": PNG}
        with patch.object(SyntheticOaWorker, "request_bytes", revoke):
            with self.assertRaises(BusinessDraftError): self.previews.fetch(**self.args)

    def test_http_response_is_no_store_and_raw_errors_do_not_expose_downstream_url(self):
        app = BusinessMaterialPreviewApplication(draft_application=BusinessDraftApplication(draft_service=self.drafts))
        response = app.get_file(self.case["input_id"], material_id=self.material["id"],
                                token=self.opened["csrfToken"], csrf_cookie=self.opened["csrfToken"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, PNG)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
        with patch.object(self.previews.drafts, "_read_downstream", side_effect=RuntimeError("private-url-and-token")):
            response = app.get_file(self.case["input_id"], material_id=self.material["id"],
                                    token=self.opened["csrfToken"], csrf_cookie=self.opened["csrfToken"])
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"private-url", response.body)


class MaterialHttpFixtureTests(unittest.TestCase):
    def test_real_http_origin_and_credentials_protect_all_synthetic_material_formats(self):
        from agentbridge.auth.reading_content import project_draft_state
        from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture
        with TemporaryDirectory() as home:
            fixture = FullBusinessAcceptanceFixture(Path(home), seed=False)
            self.addCleanup(fixture.close)
            case = fixture.add_pending_case("efficiency_data", material_previews=True)
            drafts = BusinessDraftService(fixture.service)
            opened = drafts.open(case["input_id"])
            ui = project_draft_state(opened["state"])["ui"]["review"]
            materials = [item for item in ui["materialPreviews"] if item["state"] == "available"]
            self.assertEqual({item["kind"] for item in materials}, {"image", "pdf", "archive"})
            cards, workspace = fixture.servers()
            self.addCleanup(workspace.server_close)
            thread = threading.Thread(target=cards.serve_forever, daemon=True); thread.start()
            try:
                def request(item, extra=None, *, credentials=True):
                    headers = {"Origin": fixture.card_url, "Sec-Fetch-Site": "same-origin"}
                    if credentials:
                        headers.update({"X-AgentBridge-Draft-Token": opened["csrfToken"],
                                        "Cookie": "agentbridge_csrf=" + opened["csrfToken"]})
                    headers.update(extra or {})
                    connection = http.client.HTTPConnection("127.0.0.1", cards.server_port, timeout=5)
                    try:
                        connection.request("GET", f"/flow/input/{case['input_id']}/draft/materials/{item['id']}", headers=headers)
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                    finally:
                        connection.close()
                self.assertEqual(request(materials[0], credentials=False)[0], 403)
                self.assertEqual(request(materials[0], {"Origin": "https://other.test"})[0], 403)
                self.assertEqual(request(materials[0], {"Sec-Fetch-Site": "cross-site"})[0], 403)
                self.assertEqual(fixture.oa.data.get("material_read_calls", []), [])
                for item in materials:
                    status, headers, body = request(item)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    if item["kind"] == "archive":
                        archive = json.loads(body)
                        self.assertEqual(archive["totalEntries"], 3)
                        self.assertEqual([x["state"] for x in archive["items"]], ["available", "available", "unavailable"])
                    else:
                        self.assertTrue(body.startswith(b"%PDF-") if item["kind"] == "pdf" else body.startswith(b"\x89PNG"))
                        self.assertEqual(headers["Content-Type"], "application/pdf" if item["kind"] == "pdf" else "image/png")
                self.assertEqual(len(fixture.oa.data["material_read_calls"]), 3)
                self.assertEqual(fixture.oa.writes, [])
                saved = drafts.save(case["input_id"], csrf_token=opened["csrfToken"], csrf_cookie=opened["csrfToken"],
                                    expected_revision=1, values={"opinion": "合成材料核对"})
                preview = drafts.preview(case["input_id"], csrf_token=opened["csrfToken"], csrf_cookie=opened["csrfToken"],
                                         expected_revision=saved["revision"])
                self.assertTrue(preview["ui"]["review"]["materialPreviews"])
                with sqlite3.connect(fixture.service.db_path) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone()[0], 0)
            finally:
                cards.shutdown(); cards.server_close(); thread.join(timeout=2)
