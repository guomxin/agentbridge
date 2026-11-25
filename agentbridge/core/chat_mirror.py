"""Select ordinary public text for a cross-channel display copy."""

import re
from urllib.parse import unquote

from agentbridge.adapters.yuque_content import redact_sensitive_text


NANOBOT_OA_DIGEST = "nanobot_oa_daily_digest_v1"


def private_bot_digest_kind(host: str) -> str:
    return {"nanobot": NANOBOT_OA_DIGEST, "pi": "pi_oa_daily_digest_v1"}[host]


def private_bot_chat_kind(host: str) -> str:
    return {"nanobot": "nanobot_workspace_text_v1", "pi": "pi_workspace_text_v1"}[host]



_PRIVATE = re.compile(
    r"/(?:flow/)?(?:auth|authorize|input|download|media|results|result-view)/[a-z0-9_-]+"
    r"|/api/(?:enrollment|private-cards?)/"
    r"|[?&#](?:start|ticket|presentation|token|code|signature|sig|key)=[^\s&]+"
    r"|\bBearer\s+[^\s,;]+|\b(?:set-cookie|cookie|authorization)\s*:"
    r"|io\.agentbridge/(?:interaction|telegram-confirmation)"
    r"|(?:/agentbridge\s+link|/start)\s+\S+|(?:确认码|验证码|配对码)\s*[:：]?\s*[0-9]{4,8}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)


def public_mirror_text(text: object) -> str | None:
    if not isinstance(text, str) or len(text) > 50_000 or "\0" in text:
        return None
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    text = text.strip()
    if not text or re.fullmatch(r"[0-9]{8}", text) or _PRIVATE.search(unquote(text)):
        return None
    _, secrets = redact_sensitive_text(text)
    return None if secrets else text
