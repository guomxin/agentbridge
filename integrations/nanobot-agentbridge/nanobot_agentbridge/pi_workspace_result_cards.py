"""Deliver a Pi Workspace result only to the current, authorized Telegram companion."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import re
import sqlite3
import time

from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import PiGatewayClient
from .telegram_identity import identifier
from .telegram_presentation import normalize_result_view, render_result_view_message


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return date.timestamp() if date.tzinfo is not None else None


async def handle_request(session, meta, req, path, body, origins):
    from aiohttp import web

    delivery, attempt, task = body.get("deliveryId"), body.get("attempt"), body.get("sourceTaskId")
    pairing = req.get("pairingRef")
    if req.get("hostType") != "pi" or not isinstance(pairing, str) or not re.fullmatch("[a-f0-9]{64}", pairing):
        raise web.HTTPForbidden()
    if not identifier(delivery) or not identifier(task) or type(attempt) is not int or not 1 <= attempt <= 5:
        raise web.HTTPBadRequest()
    if not path.is_file():
        raise web.HTTPForbidden()
    with sqlite3.connect(path) as db:
        claim = db.execute("SELECT run FROM notification_claims WHERE delivery=? AND endpoint=? AND attempt=?",
                           (delivery, req["endpoint"], attempt)).fetchone()
    if not claim or claim[0] != "workspace_result_view:" + task:
        raise web.HTTPForbidden()

    action = body.get("action")
    args = {"agent_host": "pi", "action": action, "endpoint_key": req["endpointKey"], "pairing_ref": pairing,
            "delivery_id": delivery, "attempt_count": attempt}
    if action == "ack":
        outcome, receipt = body.get("outcome"), body.get("receipt")
        if outcome == "sent" and type(receipt) is int and 0 < receipt < 2**53:
            args["receipt"] = receipt
        elif outcome not in ("unavailable", "delivery_unknown") or receipt is not None:
            raise web.HTTPBadRequest()
        args["outcome"] = outcome
    elif action != "issue":
        raise web.HTTPBadRequest()

    reply = await session.call_tool("agentbridge_host_workspace_result_view", args, meta=meta)
    value = reply.structuredContent
    if reply.isError or not isinstance(value, dict):
        raise web.HTTPBadGateway()
    if action == "ack":
        if type(value.get("recorded")) is not bool:
            raise web.HTTPBadGateway()
        return {"recorded": value["recorded"]}
    if value.get("available") is False:
        return {"available": False}
    view = normalize_result_view(value.get("resultView"), source_task_id=task, endpoint_id=req["endpoint"],
                                 allowed_origins=origins)
    if (value.get("available") is not True or not view or not view["selectionAvailable"]
            or timestamp(view.get("sourceExpiresAt")) is None or timestamp(value.get("leaseExpiresAt")) is None):
        raise web.HTTPBadGateway()
    return {"available": True, "resultView": view, "leaseExpiresAt": value["leaseExpiresAt"]}


@dataclass
class Claim:
    chat: int
    binding: dict
    digest: str
    delivery: str
    attempt: int
    task: str

    def payload(self):
        return {"binding": self.binding, "deliveryId": self.delivery, "attempt": self.attempt,
                "sourceTaskId": self.task}


class WorkspaceResultCards:
    """Only HTTP requests leave the poll thread. The Bot and its ledger keep a single owner."""

    def __init__(self, terminal):
        self.terminal = terminal
        self.pilot = terminal.pilot
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pi-workspace-card")
        self.pending = {}

    def _request(self, claim, action, **fields):
        payload = {**claim.payload(), "action": action, **fields}
        # This is the same bounded central issuer as a Pi-originated result view.
        # The main poller remains available, and drain() rechecks the actual lease and source expiry.
        options = {"timeout": PiGatewayClient.result_view_timeout} if action == "issue" else {}
        future = self.executor.submit(self.pilot.gateway._request, "telegram-workspace-result-view", payload, **options)
        self.pending[claim.delivery] = (claim, action, future)

    def _receipt(self, claim):
        return self.pilot.db.execute(
            "SELECT delivery,state,receipt FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=?",
            (self.pilot.bot_id, claim.chat, "result_view:" + claim.task)).fetchone()

    def _ack_known(self, claim, row):
        delivery, state, receipt = row
        if delivery == claim.delivery:
            if state == "delivered" and type(receipt) is int and 0 < receipt < 2**53:
                self._request(claim, "ack", outcome="sent", receipt=receipt)
            elif state in {"delivery_unknown", "sending"}:
                self._request(claim, "ack", outcome="delivery_unknown")

    def claim(self, chat, binding, digest, event):
        delivery, attempt, task = event.get("deliveryId"), event.get("attempt"), event.get("sourceTaskId")
        if not identifier(delivery) or not identifier(task) or type(attempt) is not int or not 1 <= attempt <= 5:
            return
        old = self.pending.get(delivery)
        if old:
            if old[0].attempt >= attempt:
                return
            del self.pending[delivery]
            old[2].cancel()
        claim = Claim(chat, dict(binding), digest, delivery, attempt, task)
        row = self._receipt(claim)
        if row:
            self._ack_known(claim, row)
        else:
            self._request(claim, "issue")

    def drain(self):
        for delivery, (claim, action, future) in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[delivery]
            try:
                value = future.result()
            except GatewayRequestError:
                # Only the central lease owner can retry. A recorded send is never repeated.
                continue
            if action == "ack":
                continue
            if value.get("available") is False:
                self._request(claim, "ack", outcome="unavailable")
                continue
            view = value.get("resultView")
            if value.get("available") is not True or not isinstance(view, dict) or view.get("sourceTaskId") != claim.task:
                continue
            deadlines = [timestamp(value.get("leaseExpiresAt")), timestamp(view.get("sourceExpiresAt")),
                         timestamp(view.get("expiresAt"))]
            if None in deadlines or time.time() >= min(deadlines) or view.get("selectionAvailable") is not True:
                continue
            from .telegram import TelegramUnavailable
            try:
                self.terminal.send_once(claim.chat, claim.binding, claim.digest, "result_view:" + claim.task,
                                        render_result_view_message(view), delivery=delivery,
                                        task_id=claim.task, send_before=min(deadlines))
            except TelegramUnavailable:
                # No Bot call was attempted if the live binding check failed to respond.
                continue
            row = self._receipt(claim)
            if row:
                self._ack_known(claim, row)
