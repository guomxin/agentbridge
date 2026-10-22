from hashlib import sha256
import json
import http.client
from threading import Thread
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from agentbridge.auth.business_draft_card import _render_page
from agentbridge.auth.pdf_assets import ROOT, PREFIX, pdf_asset_response
from tests.test_business_draft_card import draft_state
from agentbridge.auth.server import AuthServerConfig, create_auth_http_server


class PdfAssetsTests(TestCase):
    def test_vendor_files_are_pinned_and_all_declared_assets_are_available(self):
        manifest = json.loads((ROOT / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], '6.4.299')
        self.assertEqual(manifest['license'], 'Apache-2.0')
        self.assertTrue(manifest['packageIntegrity'].startswith('sha512-'))
        self.assertIn('Apache License', (ROOT / 'LICENSE').read_text())
        for name, expected in manifest['files'].items():
            with self.subTest(name=name):
                body = (ROOT / name).read_bytes()
                self.assertEqual(sha256(body).hexdigest(), expected['sha256'])
                response = pdf_asset_response(PREFIX + name)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body, body)
                self.assertEqual(response.headers['Cross-Origin-Resource-Policy'], 'same-origin')

    def test_asset_allowlist_rejects_traversal_query_and_other_versions(self):
        for path in ('../LICENSE', 'build/pdf.min.mjs?url=https://outside.test', '%2e%2e/LICENSE', 'manifest.json', 'missing.mjs'):
            with self.subTest(path=path):
                self.assertEqual(pdf_asset_response(PREFIX + path).status, 404)
        self.assertIsNone(pdf_asset_response('/assets/pdfjs/0.0.1/build/pdf.min.mjs'))
        self.assertIsNone(pdf_asset_response('/anything'))

    def test_changed_vendor_content_fails_closed(self):
        with patch.object(Path, 'read_bytes', return_value=b'changed'):
            self.assertEqual(pdf_asset_response(PREFIX + 'build/pdf.min.mjs').status, 503)

    def test_actual_inline_module_compiles_without_duplicate_bindings_or_relative_imports(self):
        # This exercises the real HTML composition, not independent module files.
        html = _render_page(draft_state(), 'synthetic', '/flow/input/' + 's'*43, 'nonce')
        script = re.search(r'<script type="module"[^>]*>(.*?)</script>', html, re.S).group(1)
        self.assertNotIn('import {renderPdfMaterial}', script)
        self.assertNotIn('from "./pdf_preview.mjs"', script)
        self.assertEqual(script.count('export async function renderPdfMaterial'), 1)
        node = shutil.which('node')
        if not node:
            self.skipTest('Node not installed; release frontend gate checks JavaScript')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'actual-card.mjs'; path.write_text(script)
            result = subprocess.run([node, '--check', str(path)], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_auth_server_serves_only_exact_local_static_get_resources(self):
        server = create_auth_http_server(config=AuthServerConfig('127.0.0.1', 0, 'http://127.0.0.1', None, None), application=Mock())
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            client.request('GET', PREFIX + 'build/pdf.min.mjs')
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'text/javascript; charset=utf-8')
            self.assertEqual(response.read(), (ROOT/'build/pdf.min.mjs').read_bytes())
            client.request('GET', PREFIX + 'build/pdf.min.mjs?source=private')
            response = client.getresponse(); self.assertEqual(response.status, 404); response.read()
            client.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
