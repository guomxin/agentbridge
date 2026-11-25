import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/native"))
from pi_model_observation import measure


class Completion:
    def __init__(self, *, origin="https://provider.example", model="vendor/text", echo=True):
        self.origin, self.model, self.echo = origin, model, echo
        self.prompts = []

    async def invoke(self, mode, **values):
        if mode == "ready":
            return {"serviceOrigin": self.origin, "model": self.model}
        assert mode == "completion" and set(values) == {"subject", "system", "message"}
        self.prompts.append(values["message"])
        return {"execution": "tool_free_completion", "model": self.model,
                "text": values["message"] if self.echo else "previous answer"}


def test_one_fresh_tool_free_completion_with_no_persisted_challenge():
    model = {"provider": "vendor", "id": "text"}
    runtime = Completion()
    first, second = [asyncio.run(measure(runtime, model, runtime.origin)) for _ in range(2)]
    assert first["toolCount"] == 0 and len(runtime.prompts) == 2
    assert first["challengeSha256"] != second["challengeSha256"]
    assert runtime.prompts[0] not in repr(first)
    for settings in ({"origin":"https://other.example"}, {"model":"vendor/different"}, {"echo": False}):
        with pytest.raises(ValueError):
            asyncio.run(measure(Completion(**settings), model, "https://provider.example"))


def test_correlate_joins_native_client_text_origin_run_and_real_inbox_shape(tmp_path):
    import json
    import sqlite3
    from pi_client_observation import correlate, read
    from common import save

    marker = "PI-" + "8" * 32
    capture = tmp_path / "view.txt"
    capture.write_text("查本人待办 " + marker + "\n两项合成待办")
    capture.chmod(0o600)
    config = tmp_path / "host.json"
    settings = {"stateDir": str(tmp_path), "instanceId": "pi-office", "bindings": [
        {"user_subject": "first"}, {"user_subject": "second"}],
        "telegramPilot": {"botId": 8801, "allowedSenders": {"first":"501"}}}
    save(config, settings)
    ledger = tmp_path / "pi-runs.sqlite3"
    binding = ["first", "e", "telegram:8801:501", "c", "tg-proof"]
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE runs(id,subject,binding,key,prompt,state,answer)")
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", ("r", "first", json.dumps(binding),
            "tg:8801:501:14", "查本人待办 " + marker, "completed", "两项合成待办"))
    ledger.chmod(0o600)
    inbox = tmp_path / "inbox.sqlite3"
    with sqlite3.connect(inbox) as db:
        db.execute("CREATE TABLE messages(bot,chat,message,run_id,subject,state,receipt)")
        db.execute("INSERT INTO messages VALUES(8801,501,14,'r','first','delivered',19)")
    inbox.chmod(0o600)
    begin = {"case": "telegram_first", "marker": marker, "startedAt": 1}
    joined = correlate(begin, capture, config, tmp_path / "proof", inbox=inbox)
    receipt = read(joined["receiptPath"])
    assert joined["client"] == "telegram_desktop" and receipt["markerSha256"] == joined["markerSha256"]
    assert read(receipt["inboxEvidence"]["path"])["replyMessageId"] == 19
    with sqlite3.connect(inbox) as db:
        db.execute("UPDATE messages SET chat=502")
    with pytest.raises(ValueError):
        correlate(begin, capture, config, tmp_path / "other", inbox=inbox)
    settings.pop("telegramPilot")
    save(config, settings)
    preparation = tmp_path / "prepared.json"
    save(preparation, {"workspaceSubjects": ["first","second"], "telegram":{"allowedPrivateSenders":{"first":"501"}}})
    with sqlite3.connect(ledger) as db:
        binding[0], binding[2] = "second", "workspace:pi-office:b"
        db.execute("UPDATE runs SET binding=?,subject='second'", (json.dumps(binding),))
    web = {**begin,"case":"workspace_second"}
    assert correlate(web, capture, config, tmp_path, inbox=None, preparation=preparation)["client"] == "chrome_extension"
    with pytest.raises(ValueError):
        correlate({**web, "case":"workspace_first"}, capture, config, tmp_path, inbox=None, preparation=preparation)
