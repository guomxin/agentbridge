from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


_STATIC = Path(__file__).resolve().parents[1] / "agentbridge" / "workspace" / "static"


class _WorkspaceShell(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.ids = set()
        self.chat_header_buttons = []
        self.chat_section = False
        self.chat_header = False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "section" and attrs.get("id") == "view-chat":
            self.chat_section = True
        if tag == "header" and self.chat_section:
            self.chat_header = True
        if tag == "button" and self.chat_header:
            self.chat_header_buttons.append(attrs.get("id"))

    def handle_endtag(self, tag):
        if tag == "header":
            self.chat_header = False
        if tag == "section":
            self.chat_section = False


class WorkspaceShellTests(unittest.TestCase):
    def test_general_conversation_header_retains_only_refresh(self):
        shell = _WorkspaceShell((_STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(shell.chat_header_buttons, ["refresh-chat"])
        self.assertTrue({"chat-form", "chat-messages", "attach-image", "refresh-chat"} <= shell.ids)

    def test_initialization_does_not_reference_removed_controls(self):
        shell = _WorkspaceShell((_STATIC / "index.html").read_text(encoding="utf-8"))
        script = (_STATIC / "workspace.js").read_text(encoding="utf-8")
        reset = script.split("function resetWorkspaceState() {", 1)[1].split("async function bootstrap()", 1)[0]
        bindings = script.split("function bindActions() {", 1)[1].split("function showAuth()", 1)[0]
        # A stale synchronous selector here would prevent login or workspace
        # initialization, even if the removed button no longer appears in HTML.
        for control_id in re.findall(r'\$\(["\']#([a-zA-Z0-9_-]+)["\']\)', reset + bindings):
            with self.subTest(control_id=control_id):
                self.assertIn(control_id, shell.ids)


if __name__ == "__main__":
    unittest.main()
