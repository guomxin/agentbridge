"""Correlate an actual client-visible challenge and response with the exact Pi run and Bot inbox."""
import argparse
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from common import external, private_file, read, save
from pi_model_observation import fixed_candidate
from pi_release import ADAPTER, SDK_VERSION, file_digest

CASES = ("workspace_first", "workspace_second", "telegram_first")


def pointer(path):
    value = private_file(path)
    return {"path": str(value), "sha256": file_digest(value)}


def fresh_destination(path):
    value = external(path)
    if value.exists() or value.is_symlink():
        raise ValueError("Observation output already exists")
    return value


def read_db(path):
    return closing(sqlite3.connect(private_file(path).resolve().as_uri() + "?mode=ro", uri=True))


def correlate(begin, ui, host_config, evidence_dir, *, inbox, preparation=None):
    marker = begin["marker"]
    if (not re.fullmatch(r"PI-[0-9a-f]{32}", marker) or begin["case"] not in CASES
            or private_file(ui).stat().st_mtime < begin["startedAt"]):
        raise ValueError("Stale or invalid client capture")
    config = read(private_file(host_config))
    subjects = {b["user_subject"] for b in config["bindings"]}
    case = begin["case"]
    pilot = config.get("telegramPilot")
    if pilot:
        senders = pilot["allowedSenders"]
    elif preparation and case != "telegram_first":
        prepared = read(private_file(preparation))
        if set(prepared["workspaceSubjects"]) != subjects:
            raise ValueError("Prepared accounts differ from the actual Web bindings")
        senders = prepared["telegram"]["allowedPrivateSenders"]
    else:
        raise ValueError("Web-only evidence requires its reviewed two-account preparation")
    if len(subjects) != 2 or len(senders) != 1 or not senders.keys() <= subjects:
        raise ValueError("Expected two Web bindings and the single approved first subject")
    first, sender = next(iter(senders.items()))
    owner = first if case != "workspace_second" else (subjects - {first}).pop()
    capture_file = private_file(ui)
    if capture_file.stat().st_size > 256_000:
        raise ValueError("Capture only this message and its client-visible reply")
    visible = " ".join(capture_file.read_text().split())
    if marker not in visible:
        raise ValueError("Unique acceptance message missing from the client observation")
    with read_db(Path(config["stateDir"]) / "pi-runs.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id,subject,binding,key,prompt,state,answer FROM runs WHERE instr(prompt,?)>0", (marker,)).fetchall()
    if len(rows) != 1:
        raise ValueError("Expected one original Pi acceptance for the observed message")
    row = rows[0]
    bound = json.loads(row["binding"])
    answer = " ".join((row["answer"] or "").split())
    if (row["state"] != "completed" or row["subject"] != owner or bound[0] != owner or not answer
            or answer not in visible or " ".join(row["prompt"].split()) not in visible):
        raise ValueError("Pi original run and visible answer do not agree")
    proof = {"observedAt": datetime.now(UTC).isoformat(), "capture": pointer(ui),
             "instanceId": config["instanceId"], "subjectSha256": hashlib.sha256(owner.encode()).hexdigest(),
             "markerSha256": hashlib.sha256(marker.encode()).hexdigest(),
             "runSha256": hashlib.sha256(row["id"].encode()).hexdigest()}
    if case != "telegram_first":
        if inbox or not bound[2].startswith("workspace:" + config["instanceId"] + ":"):
            raise ValueError("Expected the original Workspace endpoint")
        return {**proof, "client": "chrome_extension"}
    bot = int(config["telegramPilot"]["botId"])
    if not inbox or bound[2] != f"telegram:{bot}:{sender}":
        raise ValueError("Expected the reviewed first private Telegram endpoint")
    with read_db(inbox) as db:
        records = db.execute("SELECT message,state,receipt FROM messages WHERE bot=? AND chat=? AND run_id=? AND subject=?",
                             (bot, int(sender), row["id"], owner)).fetchall()
    if len(records) != 1:
        raise ValueError("Expected one private Telegram inbox record")
    message, state, receipt = records[0]
    if (row["key"] != f"tg:{bot}:{sender}:{message}" or state != "delivered" or type(receipt) is not int or receipt <= 0):
        raise ValueError("Telegram did not confirm delivery for the original private message")
    evidence_dir.mkdir(mode=0o700, exist_ok=True)
    receipt_file = fresh_destination(evidence_dir / (marker + "-delivery.json"))
    inbox_file = fresh_destination(evidence_dir / (marker + "-inbox.json"))
    save(inbox_file, {"inboundMessageId": message, "replyMessageId": receipt, "state": state,
                      "runSha256": proof["runSha256"], "markerSha256": proof["markerSha256"],
                      "botId": bot, "senderId": sender})
    save(receipt_file, {"schema": "agentbridge.pi-telegram-desktop-receipt.v1", "botId": bot, "senderId": sender,
                       "markerSha256": proof["markerSha256"], "desktopEvidence": proof["capture"], "inboxEvidence": pointer(inbox_file)})
    return {**proof, "client": "telegram_desktop", "transport": "telegram_bot_api",
            "receiptPath": str(receipt_file), "receiptSha256": file_digest(receipt_file)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    start = modes.add_parser("begin")
    start.add_argument("--case", choices=CASES, required=True)
    start.add_argument("--output", required=True)
    finish = modes.add_parser("finish")
    for arg in ("begin", "capture", "host-config", "output"):
        finish.add_argument("--" + arg, required=True)
    finish.add_argument("--preparation", help="Owner-only reviewed account preparation for a web-only profile")
    finish.add_argument("--inbox", help="Owner-only live Telegram consumer's SQLite database")
    args = parser.parse_args()
    output = fresh_destination(args.output)
    candidate = fixed_candidate()
    if args.mode == "begin":
        marker = "PI-" + secrets.token_hex(16)
        save(output, {"schema":"agentbridge.pi-client-challenge.v1", **candidate, "case":args.case, "marker":marker,
                      "startedAt":time.time()})
        print(marker)
        return
    begin = read(private_file(args.begin))
    if begin.get("schema") != "agentbridge.pi-client-challenge.v1" or any(begin.get(k) != v for k,v in candidate.items()):
        raise ValueError("Client capture belongs to another candidate")
    proof = correlate(begin, args.capture, args.host_config, output.parent, inbox=args.inbox, preparation=args.preparation)
    save(output, {"schema":"agentbridge.pi-observation.v1","case":begin["case"],"passed":True,
                  "implementation":"pi","adapterVersion":ADAPTER,"sdkVersion":SDK_VERSION,**candidate,**proof})
    print("Client response and original Pi receipt verified.")


if __name__ == "__main__":
    main()
