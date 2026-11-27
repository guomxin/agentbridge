"""Public mirrors share one character cap and preserve Telegram-safe parts."""
from html import escape

import pytest

from agentbridge.core.chat_mirror import (
    PUBLIC_TEXT_MAX_CHARACTERS, PUBLIC_TEXT_PART_UTF16_UNITS,
    public_mirror_text, split_public_text,
)
from agentbridge.core.tasks import WORKSPACE_RESPONSE_MAX_CHARACTERS


@pytest.mark.parametrize("length", [49_999, 50_000, 50_001, 199_999, 200_000])
def test_public_text_preserves_code_points_and_escaped_utf16_budget(length):
    text = ("中文😀<&>\"'*_[链接](https://example.invalid)\n" * 6_000)[:length - 1] + "尾"
    assert len(text) == length
    assert public_mirror_text(text, maximum=PUBLIC_TEXT_MAX_CHARACTERS) == text
    parts = split_public_text(text)
    assert "".join(parts) == text
    for number, part in enumerate(parts, 1):
        assert part
        assert len(escape(part).encode("utf-16-le")) // 2 <= PUBLIC_TEXT_PART_UTF16_UNITS
        rendered = f"工作台回复（{number}/{len(parts)}）\n" + escape(part)
        assert len(rendered.encode("utf-16-le")) // 2 < 4096
    assert PUBLIC_TEXT_MAX_CHARACTERS == WORKSPACE_RESPONSE_MAX_CHARACTERS


def test_public_text_rejects_overflow_without_truncating_or_hiding_a_private_suffix():
    overflow = "😀" * (PUBLIC_TEXT_MAX_CHARACTERS + 1)
    assert public_mirror_text(overflow, maximum=PUBLIC_TEXT_MAX_CHARACTERS) is None
    with pytest.raises(ValueError, match="200000"):
        split_public_text(overflow)
    hidden = "公开文字" * 49_000 + "/results/private-synthetic-ticket"
    assert len(hidden) < PUBLIC_TEXT_MAX_CHARACTERS
    assert public_mirror_text(hidden, maximum=PUBLIC_TEXT_MAX_CHARACTERS) is None


@pytest.mark.parametrize("invalid", ["", None, "\ud800"])
def test_public_text_does_not_create_empty_or_invalid_unicode_parts(invalid):
    with pytest.raises((ValueError, UnicodeEncodeError)):
        split_public_text(invalid)
