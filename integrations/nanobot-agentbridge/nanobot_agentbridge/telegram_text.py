"""Split Telegram plain text at its UTF-16 limit without cutting a Unicode character."""


def split_telegram_text(text, *, prefix=""):
    start = 0
    size = len(prefix.encode("utf-16-le")) // 2
    for index, character in enumerate(text):
        units = 2 if ord(character) > 0xffff else 1
        if size + units > 4096:
            yield prefix + text[start:index]
            start, prefix, size = index, "", 0
        size += units
    yield prefix + text[start:]
