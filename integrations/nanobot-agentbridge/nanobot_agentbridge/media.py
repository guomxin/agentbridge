"""Validate Workspace image bytes and stage only generated, local paths for NanoBot."""
import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_FILE = 6 * 1024 * 1024
MAX_TOTAL = 12 * 1024 * 1024
FORMATS = {"image/png": (".png", b"\x89PNG\r\n\x1a\n"),
           "image/jpeg": (".jpg", b"\xff\xd8\xff"), "image/webp": (".webp", b"RIFF")}


@dataclass(frozen=True)
class ImageUpload:
    suffix: str
    name: str
    data: bytes

    def identity(self):
        return [self.suffix, self.name, hashlib.sha256(self.data).hexdigest()]


def validate_images(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 4:
        raise ValueError("Invalid image count")
    images = []
    total = 0
    for item in values:
        if not isinstance(item, dict) or item.get("type") != "image":
            raise ValueError("Expected a Workspace image")
        suffix, signature = FORMATS[item["mimeType"]]
        content, name = item["content"], item["fileName"]
        if not isinstance(content, str) or not 0 < len(content) <= 4 * ((MAX_FILE + 2) // 3):
            raise ValueError("Invalid image size")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise ValueError("Invalid image name")
        try:
            data = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid image encoding") from None
        total += len(data)
        if (len(data) > MAX_FILE or total > MAX_TOTAL or not data.startswith(signature)
                or (suffix == ".webp" and data[8:12] != b"WEBP")):
            raise ValueError("Invalid image payload")
        images.append(ImageUpload(suffix, name, data))
    return images


def clear_stale_images(directory: Path) -> None:
    # This directory is generated exclusively for the adapter. NanoBot keeps an
    # additional data-URL cache after it saves SDK history. Subsequent model
    # turns must obtain images again from the central attachment store.
    for path in directory.glob("image-run-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    cache = directory / "session-images"
    if cache.is_dir() and not cache.is_symlink():
        for path in cache.iterdir():
            if re.fullmatch("[a-f0-9]{64}", path.name):
                path.unlink(missing_ok=True)


@contextmanager
def staged_images(parent: Path, images):
    if not images:
        yield []
        return
    with TemporaryDirectory(dir=parent, prefix="image-run-") as folder:
        paths = []
        for index, image in enumerate(images):
            path = Path(folder) / f"{index}{image.suffix}"
            path.touch(mode=0o600, exist_ok=False)
            path.write_bytes(image.data)
            paths.append(str(path))
        try:
            yield paths
        finally:
            cache = parent / "session-images"
            for image in images:
                mime = next(mime for mime, (suffix, _) in FORMATS.items() if suffix == image.suffix)
                digest = hashlib.sha256(f"data:{mime};base64,".encode() + base64.b64encode(image.data)).hexdigest()
                (cache / digest).unlink(missing_ok=True)
