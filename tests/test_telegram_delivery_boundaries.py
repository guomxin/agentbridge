"""A full Bot API rejection is distinct from a lost or malformed send receipt."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge.telegram import TelegramAPI, TelegramRejected, TelegramUnavailable


@pytest.mark.parametrize("status,body,code,delay", [
    (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 9}}, 429, 9),
    (400, {"ok": False, "error_code": 400, "description": "invalid request"}, 400, None),
    (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": True}}, 429, None),
    (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 86401}}, 429, None),
    (500, {"ok": False, "error_code": 500}, None, None),
    (429, {"ok": False, "error_code": 400}, None, None),
    (429, {"ok": True, "result": {}}, None, None),
    (429, "malformed", None, None),
])
def test_bot_api_only_classifies_complete_matching_rejections(status, body, code, delay):
    raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    sent = []
    connection = SimpleNamespace(request=lambda *args: sent.append(True), close=lambda: None,
                                 getresponse=lambda: SimpleNamespace(status=status, read=lambda _: raw, will_close=True))
    api = TelegramAPI("fixture-token", connection_factory=lambda: connection)
    with pytest.raises(TelegramUnavailable) as caught:
        api.call("sendMessage", chat_id=501, text="synthetic result")
    assert len(sent) == 1
    assert isinstance(caught.value, TelegramRejected) is (code is not None)
    if code:
        assert caught.value.error_code == code and caught.value.retry_after == delay
    assert "fixture-token" not in str(caught.value)
