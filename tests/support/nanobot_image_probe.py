"""Verify the exact authorized pixels on the SDK's OpenAI-compatible boundary."""
import asyncio
import base64
import json
from pathlib import Path
import struct
import zlib


def sample_png(rgb):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" + bytes(rgb))) + chunk(b"IEND", b""))


class ImageScenario:
    def __init__(self, secret):
        self.secret = secret
        self.failures = []
        self.seen = []

    def reply(self, body):
        if self.secret in json.dumps(body):
            raise ValueError("Unexpected model credentials")
        urls = [block["image_url"]["url"] for m in body["messages"] if isinstance(m.get("content"), list)
                for block in m["content"] if block.get("type") == "image_url"]
        self.seen.append(urls)
        return "已检查本次图片。" if urls else "本次没有图片。", None


async def verify_images(client, bindings, scenarios, root):
    rgb = [(255, 0, 0), (0, 0, 255)]
    for i, binding in enumerate(bindings):
        data = base64.b64encode(sample_png(rgb[i])).decode()
        image = {"type":"image", "mimeType":"image/png", "fileName":"synthetic.png","content":data}
        def send(key, images):
            return list(client.send_dispatch({"idempotency_key":key}, binding,"核对合成像素",images,None))
        events = await asyncio.to_thread(send, "image", [image])
        assert events[-1]["text"] == "已检查本次图片。", events[-1]["state"]
        assert scenarios[i].seen == [["data:image/png;base64," + data]]
        again = await asyncio.to_thread(send, "image", [image])
        assert again[0]["runId"] == events[0]["runId"] and len(scenarios[i].seen) == 1
        followup = await asyncio.to_thread(send, "next", [])
        assert followup[-1]["text"] == "本次没有图片。" and scenarios[i].seen[-1] == []
        assert not any(p.is_file() for p in Path(root).glob("**/session-images/*"))
    return {"exact_uploaded_pixels":2, "copies_after_run":0, "duplicates_reused":2, "text_only_followups":2}
