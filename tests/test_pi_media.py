"""Exercise the shared attachment policy and the separately installed Pi document reader."""
import base64
import hashlib
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "integrations/pi-agentbridge"), str(ROOT / "integrations/nanobot-agentbridge")]
from pi_agentbridge.media import decode_inputs


def item(kind, mime, raw):
    return {"type": kind, "mimeType": mime, "fileName": "sample", "content": base64.b64encode(raw).decode()}


def test_plain_documents_are_transient_bounded_and_private_chat_only():
    data = ("材料" * 12500).encode()
    entry = item("document", "text/csv", data)
    images, receipts, text = decode_inputs([entry], telegram=True)
    assert not images and receipts == [[".csv", "sample", hashlib.sha256(data).hexdigest()]]
    assert "材料" * 12000 in text and "材料" * 12001 not in text and '"truncated": true' in text
    for bad in (item("document", "text/plain", b"bad\x00"), item("document", "text/plain", b"\xff")):
        with pytest.raises(ValueError):
            decode_inputs([bad], telegram=True)
    with pytest.raises(ValueError):
        decode_inputs([entry], telegram=False)


def test_independent_image_and_pdf_engine(monkeypatch):
    python = os.environ.get("AGENTBRIDGE_PI_TEST_PYTHON")
    if python:
        monkeypatch.setattr(sys, "executable", python)
    else:
        pytest.importorskip("pypdf")
        pytest.importorskip("PIL")
    from tests.support.card_material_acceptance import scanned_pdf, text_pdf
    from tests.support.nanobot_image_probe import sample_png

    first, second = [item("image", "image/png", sample_png(rgb)) for rgb in ((255, 0, 0), (0, 0, 255))]
    pdf = item("document", "application/pdf", text_pdf(pages=2))
    pictures, fingerprints, excerpt = decode_inputs([first, pdf, second], telegram=True)
    assert [a["data"] for a in pictures] == [first["content"], second["content"]]
    assert [e[0] for e in fingerprints] == [".png", ".pdf", ".png"]
    assert "UNIQUE-FOOTER-02" in excerpt and not any(k in excerpt for k in (first["content"], second["content"]))
    assert decode_inputs([], telegram=False) == ([], [], "")
    for invalid in (item("document", "application/pdf", scanned_pdf()), item("image", "image/png", b"\x89PNG\r\n\x1a\ninvalid")):
        with pytest.raises(ValueError):
            decode_inputs([invalid], telegram=True)
