"""Loopback Bot API → pinned NanoBot SDK → model with synthetic per-user media."""

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from tests.support.card_material_acceptance import text_pdf
from tests.support.nanobot_b4_contract import environment
from tests.support.nanobot_b4_files import login
from tests.support.nanobot_image_probe import sample_png
from tests.support.nanobot_telegram_loopback import fixture, update


SYNTHETIC_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAEAAQDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAABQb/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCeAFQb/9k="
)


def run(python):
    with environment(python) as home:
        info = json.loads((home / "public.json").read_text())
        gateway = NanoBotGatewayClient(
            url=f"http://127.0.0.1:{info['worker_port']}",
            instance_id="nanobot-office",
            token_file=home / "service.token",
        )
        with login(info["signin"]["b"], "pilotb") as web:
            draft = web.post(
                "/api/hosts/telegram/begin", json={"instance_id": "nanobot-office"}
            )
            draft.raise_for_status()
            pairing = draft.json()
            code = gateway.present_telegram_challenge(8001, 502, pairing["code"])
            confirmed = web.post(
                "/api/hosts/telegram/confirm",
                json={
                    "instance_id": "nanobot-office",
                    "challenge_id": pairing["challenge_id"],
                    "confirmation_code": code,
                },
            )
            confirmed.raise_for_status()
        folder = home / "independent-media-probe"
        folder.mkdir(mode=0o700)
        with fixture(
            folder, actual_gateway=gateway, actual_bindings={}, allow_pairing=True
        ) as (state, _, create):
            pilot = create()
            try:
                for number, (uid, data, mime, expected) in enumerate(
                    (
                        (501, sample_png((255, 0, 0)), "image/png", None),
                        (501, SYNTHETIC_JPEG, "image/jpeg", None),
                        (502, sample_png((0, 0, 255)), "image/png", None),
                        (
                            501,
                            b"SYNTHETIC TEXT MEDIA-SAMPLE-481",
                            "text/plain",
                            "合成文档内容已读 481",
                        ),
                        (
                            501,
                            text_pdf(pages=1),
                            "application/pdf",
                            "合成 PDF 第 1 页已读",
                        ),
                    ),
                    81,
                ):
                    file_id = f"synthetic_{number}"
                    state["files"][file_id] = {
                        "file_path": f"documents/{file_id}.dat",
                        "bytes": data,
                    }
                    packet = update(
                        number,
                        uid,
                        number,
                        document={"file_id": file_id, "mime_type": mime},
                        caption="核对我本次发送的附件。",
                    )
                    if mime == "image/jpeg":
                        del packet["message"]["document"]
                        del packet["message"]["caption"]
                        packet["message"]["photo"] = [
                            {
                                "file_id": "unselected_thumbnail",
                                "width": 1,
                                "height": 1,
                            },
                            {
                                "file_id": file_id,
                                "width": 4,
                                "height": 4,
                                "file_size": len(data),
                            },
                        ]
                    del packet["message"]["text"]
                    pilot.ingest(packet)
                    assert pilot._record(uid, number) is not None, (
                        "No private binding",
                        uid,
                    )
                    end = time.monotonic() + 35
                    while time.monotonic() < end:
                        pilot.reconcile()
                        if pilot._record(uid, number)["state"] == "delivered":
                            break
                        time.sleep(0.15)
                    matched = [
                        answer["text"]
                        for answer in state["deliveries"]
                        if answer["chat_id"] == uid
                        and answer.get("reply_parameters", {}).get("message_id")
                        == number
                    ]
                    assert (
                        len(matched) == 1
                        and (expected or hashlib.sha256(data).hexdigest()[:8])
                        in matched[0]
                    ), (number, matched)
                    assert all(
                        item["chat_id"] == uid
                        for item in state["deliveries"]
                        if item.get("reply_parameters", {}).get("message_id") == number
                    )
                    pilot.ingest(packet)
                assert len(state["downloads"]) == 5
                assert not list((home / "b4host/state").rglob("image-run-*"))
            finally:
                pilot.db.close()
        return {
            "telegramDownloads": 5,
            "sdkImageDigests": 3,
            "textFilesRead": 1,
            "pdfsRead": 1,
            "duplicateDownloads": 0,
            "otherUserDeliveries": 0,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", type=Path, required=True)
    print(json.dumps(run(p.parse_args().nanobot_python)))
