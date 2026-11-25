"""Pinned Telegram downloads, attachment dispatch and sender isolation over loopback HTTP."""

import base64

import pytest

from tests.support.nanobot_image_probe import sample_png
from tests.support.nanobot_telegram_loopback import fixture, update
from nanobot_agentbridge.media import MAX_FILE, validate_attachments


def incoming(number, **content):
    message = update(number, message=number, **content)
    del message["message"]["text"]
    return message


def test_document_png_and_caption_reach_only_original_run(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        data = sample_png((255, 0, 0))
        state["files"]["red"] = {"file_path": "documents/file_20.png", "bytes": data}
        state["updates"] = [
            incoming(
                1,
                caption="哪种颜色？",
                document={
                    "file_id": "red",
                    "mime_type": "image/png",
                    "file_name": "../external",
                },
            )
        ]
        pilot.poll_once()
        requests = [body for path, body in state["calls"] if path == "/v1/accept"]
        assert len(requests) == 1 and requests[0]["message"] == "哪种颜色？"
        assert requests[0]["attachments"] == [
            {
                "type": "image",
                "mimeType": "image/png",
                "fileName": "attachment.png",
                "content": base64.b64encode(data).decode(),
            }
        ]
        assert requests[0]["binding"]["user_subject"] == "alpha"
        assert len(state["downloads"]) == 1
        pilot.ingest(state["updates"][0])
        assert len(state["downloads"]) == 1
        pilot.db.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"voice": {"file_id": "v"}},
        {"document": {"file_id": "v", "mime_type": "application/zip"}},
        {
            "document": {
                "file_id": "v",
                "mime_type": "image/png",
                "file_size": MAX_FILE + 1,
            }
        },
        {
            "document": {"file_id": "v", "mime_type": "text/plain"},
            "caption": "/approve",
        },
    ],
)
def test_unsupported_or_excessive_media_gets_a_receipt_without_download(
    tmp_path, extra
):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        pilot.ingest(incoming(1, **extra))
        assert (
            len(state["deliveries"]) == 1
            and not state["admissions"]
            and not state["downloads"]
        )
        pilot.db.close()


def test_unbound_and_tainted_file_reference_never_download(tmp_path):
    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        state["files"]["bad"] = {
            "file_path": "https://example.invalid/foreign.png",
            "bytes": b"example",
        }
        file = {"file_id": "bad", "mime_type": "image/png"}
        pilot.ingest(incoming(1, document=file, forward_origin={}))
        state["verified"].clear()
        pilot.ingest(incoming(2, document=file))
        assert not any(path.endswith("getFile") for path, _ in state["calls"])
        state["verified"].add("501")
        pilot.ingest(incoming(3, document=file))
        assert (
            not state["downloads"]
            and not state["admissions"]
            and len(state["deliveries"]) == 1
        )
        pilot.db.close()


def test_workspace_does_not_acquire_document_uploads():
    data = [
        {
            "type": "document",
            "mimeType": "text/plain",
            "fileName": "test.txt",
            "content": "YQ==",
        }
    ]
    assert validate_attachments(data, telegram=True)[0].data == b"a"
    with pytest.raises(ValueError):
        validate_attachments(data)
