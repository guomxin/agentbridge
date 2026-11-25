"""Isolated stdin-only attachment parser. Stdout holds bounded extracted text."""
import io
import json
import resource
import sys

LIMIT = 24_000


def read_pdf(stream):
    from pypdf import PdfReader, overwrite_configuration

    cap = 8 * 1024**2
    overwrite_configuration(maximum_declared_stream_length=cap, array_based_stream_maximum_output_length=cap,
        lzw_maximum_output_length=cap, run_length_maximum_output_length=cap, zlib_maximum_output_length=cap,
        brotli_maximum_output_length=cap, image_maximum_buffer_size=cap, jbig2dec_binary=None,
        stream_decoding_work_maximum_length=4 * cap, stream_parsing_work_maximum_count=100_000,
        page_tree_maximum_entries=2000, page_tree_maximum_page_entries=200, xform_maximum_invocations_per_extraction=100)
    pdf = PdfReader(stream, strict=True, xref_stream_maximum_entries=40_000)
    if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 200:
        raise ValueError("Unreadable PDF")
    text = ""
    for page in pdf.pages:
        text += (page.extract_text() + "\n")[: LIMIT + 1 - len(text)]
        if len(text) > LIMIT:
            break
    return text


def main(suffix):
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    raw = sys.stdin.buffer.read(6 * 1024**2 + 1)
    if len(raw) > 6 * 1024**2:
        raise ValueError("Oversized attachment")
    formats = {".png": "PNG", ".jpg": "JPEG", ".webp": "WEBP"}
    if suffix in formats:
        from PIL import Image

        with Image.open(io.BytesIO(raw), formats=[formats[suffix]]) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Oversized image")
            image.verify()
        return
    text = read_pdf(io.BytesIO(raw)) if suffix == ".pdf" else raw.decode("utf-8-sig")
    if not text.strip() or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        raise ValueError("Unreadable document")
    print(json.dumps({"format": suffix[1:], "text": text[:LIMIT], "truncated": len(text) > LIMIT}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
