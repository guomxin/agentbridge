"""Exercise the actual self-hosted bridge without granting it any form API."""
import json
import re
import shutil
import subprocess
import unittest

from agentbridge.auth.embedded import EMBEDDED_SAFE_AREA_CSS, render_embedded_web_app_bridge
from agentbridge.auth.field_card import _document


class EmbeddedWebAppTests(unittest.TestCase):
    def run_bridge(self, *, complete=False):
        rendered = render_embedded_web_app_bridge(nonce="test-nonce", close_when_complete=complete)
        script = re.search(r'<script nonce="test-nonce">(.*?)</script>', rendered, re.S)[1]
        # The VM intentionally exposes no form nodes, fetch, storage or host
        # SDK. Any attempt to use them fails rather than leaking sample values.
        runner = r"""
import assert from 'node:assert/strict';
import {runInNewContext} from 'node:vm';
const source = JSON.parse(process.argv[1]);
function launch(embedded) {
  const posts = [], timers = [], properties = {}, events = {}, viewportEvents = {};
  const document = {documentElement: {dataset: {}, style: {
    setProperty: (name, value) => { properties[name] = value; }
  }}};
  const window = {location: {hash: embedded ? '#tgWebAppPlatform=ios' : ''}, innerHeight: 700,
    visualViewport: {height: 420, addEventListener: (name, fn) => { viewportEvents[name] = fn; }},
    addEventListener: (name, fn) => { events[name] = fn; },
    setTimeout: (fn, delay) => { timers.push({fn, delay}); },
    ...(embedded ? {TelegramWebviewProxy: {postEvent: (name, data) => posts.push({name, data: JSON.parse(data)})}} : {})};
  runInNewContext(source, {window, document, URLSearchParams});
  return {window, document, properties, events, viewportEvents, posts, timers};
}
const ordinary = launch(false);
assert.deepEqual(ordinary.posts, []);
assert.deepEqual(ordinary.properties, {});
assert.equal(ordinary.document.documentElement.dataset.agentHost, undefined);
const embedded = launch(true);
assert.equal(embedded.document.documentElement.dataset.agentHost, 'telegram');
assert.equal(embedded.properties['--agent-viewport-height'], '420px');
assert.deepEqual(embedded.posts, [{name: 'web_app_ready', data: {}}, {name: 'web_app_expand', data: {}}]);
embedded.window.visualViewport.height = 260;
embedded.viewportEvents.resize();
assert.equal(embedded.properties['--agent-viewport-height'], '260px');
embedded.window.visualViewport.height = 390;
embedded.events.pageshow();
assert.equal(embedded.properties['--agent-viewport-height'], '390px');
embedded.window.visualViewport.height = Infinity;
embedded.viewportEvents.resize();
assert.equal(embedded.properties['--agent-viewport-height'], '390px');
if (JSON.parse(process.argv[2])) {
  assert.equal(embedded.timers.length, 1);
  assert.equal(embedded.timers[0].delay, 1200);
  embedded.timers[0].fn();
  assert.deepEqual(embedded.posts[2], {name: 'web_app_close', data: {}});
} else assert.equal(embedded.timers.length, 0);
console.log(JSON.stringify({posts: embedded.posts.map(item => item.name)}));
"""
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required by the shared frontend validation")
        result = subprocess.run([node, "--input-type=module", "-e", runner, json.dumps(script), json.dumps(complete)],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_viewport_geometry_and_restore_never_read_or_send_business_fields(self):
        self.assertEqual(self.run_bridge()["posts"], ["web_app_ready", "web_app_expand"])

    def test_only_explicit_completed_documents_can_request_close(self):
        self.assertEqual(self.run_bridge(complete=True)["posts"],
                         ["web_app_ready", "web_app_expand", "web_app_close"])

    def test_mobile_document_has_touch_targets_safe_material_scrolling_and_close_explanation(self):
        html = _document(title="合成办理页", nonce="test-nonce", body="<main>合成</main>")
        self.assertIn("font-size:16px", html)
        self.assertIn("min-height:44px", html)
        self.assertIn("overflow-x: auto", EMBEDDED_SAFE_AREA_CSS)
        self.assertIn("--agent-viewport-height", EMBEDDED_SAFE_AREA_CSS)
        self.assertIn("关闭此窗口不会取消原任务", html)
        self.assertNotIn("telegram.org", html)
        self.assertNotIn("sendData", html)
        self.assertNotIn("MainButton", html)
        self.assertNotIn("localStorage", html)


if __name__ == "__main__":
    unittest.main()
