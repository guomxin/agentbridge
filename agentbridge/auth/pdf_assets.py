"""Exact immutable local PDF.js resources; never proxy a caller-supplied URL."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from agentbridge.auth.card import AuthCardResponse

VERSION = "6.4.299"
PREFIX = f"/assets/pdfjs/{VERSION}/"
ROOT = Path(__file__).with_name("static") / "vendor" / "pdfjs" / VERSION


@lru_cache(maxsize=1)
def _manifest():
    return json.loads((ROOT / "manifest.json").read_text())["files"]


def pdf_asset_response(path):
    if not path.startswith(PREFIX):
        return None
    name = path[len(PREFIX):]
    entry = _manifest().get(name)
    if entry is None:
        return AuthCardResponse(404, {"Content-Type": "text/plain", "Cache-Control": "no-store"}, b"Not found")
    body = (ROOT / name).read_bytes()
    if len(body) != entry["bytes"] or sha256(body).hexdigest() != entry["sha256"]:
        return AuthCardResponse(503, {"Content-Type": "text/plain", "Cache-Control": "no-store"}, b"Asset integrity check failed")
    content_type = "text/javascript; charset=utf-8" if name.endswith((".mjs", ".js")) else (
        "application/wasm" if name.endswith(".wasm") else "application/octet-stream")
    return AuthCardResponse(200, {"Content-Type": content_type, "Content-Length": str(len(body)),
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; worker-src 'none'"}, body)
