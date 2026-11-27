"""Private-chat command and confirmed-receipt routing for existing Telegram tasks."""

import json
import re
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from agentbridge.core.chat_mirror import public_mirror_text
from agentbridge.workspace.gateway import GatewayRequestError

from .chat_mirror import identifier
from .telegram_presentation import render_interaction_message, render_result_view_message


_COMMANDS = {
    "/pending": "list", "/agentbridge pending": "list", "找回原卡": "list",
    "找回卡片": "list", "查看待处理任务": "list",
    "/continue": "continue", "继续": "continue", "继续办理": "continue", "继续原任务": "continue",
    "/stop": "stop", "/cancel": "stop", "停止": "stop", "停止任务": "stop", "取消任务": "stop",
}
_STATES = {
    "waiting_user": "等待本人操作", "active": "进行中", "running": "执行中",
    "succeeded": "已完成", "partially_succeeded": "部分事项已完成，请核对原任务",
    "failed": "未完成", "canceled": "已停止",
    "superseded": "原入口已关闭", "expired": "已过期", "outcome_unknown": "业务结果待核实",
    "unknown": "业务结果待核实",
}


def command(text, reply):
    action = _COMMANDS.get(text)
    if action:
        return action, None
    if reply:
        choice = re.fullmatch(r"(?:(继续|停止|找回原卡)\s*)?([1-9][0-9]?)", text)
        if choice:
            return _COMMANDS.get(choice[1]), int(choice[2])
    return None


def _local_time(value):
    try:
        created = datetime.fromisoformat(value)
        if created.tzinfo is not None:
            return " · " + created.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    return ""


class TelegramNavigation:
    def __init__(self, pilot):
        self.pilot = pilot
        with pilot.db:
            pilot.db.execute("""CREATE TABLE IF NOT EXISTS telegram_navigation_receipts(
                bot INTEGER, chat INTEGER, receipt INTEGER, digest TEXT NOT NULL, target_json TEXT NOT NULL,
                PRIMARY KEY(bot,chat,receipt))""")

    def remember(self, chat, receipt, digest, target):
        if receipt and target:
            self.pilot.db.execute("INSERT OR IGNORE INTO telegram_navigation_receipts VALUES(?,?,?,?,?)",
                                  (self.pilot.bot_id, chat, receipt, digest, json.dumps(target)))

    def quoted(self, chat, digest, reply):
        pilot = self.pilot
        if (not isinstance(reply, dict) or not isinstance(reply.get("from"), dict) or reply["from"].get("id") != pilot.bot_id
                or type(reply.get("message_id")) is not int
                or ("chat" in reply and (not isinstance(reply["chat"], dict) or (reply["chat"].get("type"), reply["chat"].get("id")) != ("private", chat)))
                or any(k in reply for k in ("forward_origin", "sender_chat", "via_bot"))):
            return None
        key = (pilot.bot_id, chat, reply["message_id"])
        saved = pilot.db.execute(
            "SELECT digest,target_json FROM telegram_navigation_receipts WHERE bot=? AND chat=? AND receipt=?", key
        ).fetchone()
        if saved:
            if saved[0] != digest:
                return None
            target = json.loads(saved[1])
            if target.get("kind") == "run" and identifier(target.get("terminalTaskId")):
                return {"kind": "history_task", "id": target["terminalTaskId"]}
            return target
        # Older ordinary replies already have the binding digest and exact original run.
        rows = pilot.db.execute("""SELECT DISTINCT m.message,m.run_id,m.binding_hash FROM messages m
            LEFT JOIN telegram_reply_parts p ON (p.bot,p.chat,p.message)=(m.bot,m.chat,m.message)
            WHERE m.bot=? AND m.chat=? AND (m.receipt=? OR p.receipt=?) AND m.run_id IS NOT NULL""",
            (*key, key[-1])).fetchall()
        # Previously delivered cards have opaque IDs only. The host verifies original
        # endpoint, task identity, current binding and original conversation before use.
        cards = pilot.db.execute("""SELECT dedupe_key,delivery,state FROM telegram_card_deliveries
            WHERE bot=? AND chat=? AND receipt=?""", key).fetchall()
        if rows:
            if len(rows) == 1 and not cards and rows[0][2] == digest:
                return {"kind": "run", "id": rows[0][1], "requestKey": pilot._key(chat, rows[0][0])}
            return None
        if len(cards) == 1 and cards[0][2] == "delivered":
            prefix, _, ident = cards[0][0].partition(":")
            if prefix == "status" and ident == cards[0][1] and identifier(ident):
                return {"kind": "status_delivery", "id": ident}
            if prefix in {"interaction", "result_view"}:
                return {"kind": "interaction" if prefix == "interaction" else "result", "id": ident}
        if not cards and reply.get("chat", {}).get("id") == chat:
            from .notifications import TerminalMessages
            text = reply.get("text")
            if isinstance(text, str) and (text in TerminalMessages.TEXT.values() or text.splitlines()[:1] == ["办理状态更新"]):
                # Text merely opens a chooser. It cannot establish any task as the quoted one.
                return {"kind": "legacy_history"}
        return None

    def handle(self, chat, message, binding, digest, reply, verb, ordinal):
        pilot = self.pilot

        def send(text, *, target=None, formatted=None, native_source=None):
            active, proof = pilot._binding(chat)
            if active != binding or proof != digest:
                pilot._state(chat, message, "binding_changed")
                return
            pilot._state(chat, message, "sending")
            params = formatted or {}
            key = "interaction:" + str((native_source or {}).get("interactionId"))
            inserted = False
            if native_source:
                with pilot.db:
                    inserted = pilot.db.execute("INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,NULL,'sending',NULL)",
                                                (pilot.bot_id, chat, key)).rowcount
                    if inserted:
                        pilot.native_card_closures.arm(chat, digest, key, native_source)
            receipt = pilot._confirmed_send(chat, message, text, parse_mode=params.get("parse_mode"),
                                            reply_markup=params.get("reply_markup"))
            with pilot.db:
                if inserted:
                    pilot.db.execute("UPDATE telegram_card_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND dedupe_key=?",
                                     ("delivered" if receipt else "delivery_unknown",receipt,pilot.bot_id,chat,key))
                self.remember(chat, receipt, digest, target)
                pilot.db.execute("UPDATE messages SET state=?, receipt=? WHERE bot=? AND chat=? AND message=?",
                                 ("delivered" if receipt else "delivery_unknown", receipt, pilot.bot_id, chat, message))

        target = self.quoted(chat, digest, reply) if reply is not None else None
        if reply is not None and not target:
            send("无法核对这条机器人消息的原任务。发送 /pending 查看本人当前任务。")
            return
        kind = target.get("kind") if target else None
        if kind == "workspace_public_message":
            if verb != "continue" or ordinal:
                send("这条消息可以引用查看。请回复原消息并发送“继续”。")
                return
            try:
                response = pilot.gateway._request("telegram-task", {"binding": binding, "operation": "public_reference",
                    "target": {"telegramReceipt": target["receipt"]}})
            except GatewayRequestError:
                response = {}
            send(public_mirror_text(response.get("referenceText")) or "暂时无法核对这条原消息。")
            return
        if kind in {"legacy_history", "history_choices", "history_task", "status_delivery"} and verb == "stop":
            send("旧状态通知只可查看。要停止待处理任务，请发送 /pending 并核对原任务。")
            return
        if kind == "history_choices":
            if time.time() >= target["expires"]:
                send("这份历史任务列表已过期，请重新回复旧通知并发送“继续”。")
                return
            if verb not in {None, "continue"} or not ordinal or ordinal > len(target["ids"]):
                send("请回复这份历史任务列表并输入要查看的编号。")
                return
            verb = "continue"
            target = {"kind": "history_task", "id": target["ids"][ordinal - 1]}
        elif target and target["kind"] == "choices":
            ids = target["ids"]
            if time.time() >= target["expires"]:
                send("这份列表已过期，请重新发送 /pending 或“停止”获取当前任务。")
                return
            if (not ordinal or ordinal > len(ids)
                    or (verb is not None and verb != target["action"] and verb != "list")):
                send("请回复原列表并输入其中的编号；如需切换操作，请另发 /pending 或“停止”。")
                return
            verb = target["action"]
            target = {"kind": "task", "id": ids[ordinal - 1]}
        elif ordinal:
            send("请回复包含编号的任务列表，再输入对应编号。")
            return
        if verb == "list":
            verb = "continue"
        request = {"binding": binding}
        try:
            result = None
            history = kind == "legacy_history"
            if kind == "status_delivery":
                result = pilot.gateway._request("telegram-task", {**request, "operation": verb, "target": target})
                history = result.get("state") == "unavailable"
            if not target or history:
                reply_data = pilot.gateway._request("telegram-task", {**request, "operation": "history" if history else "list"})
                choices = reply_data.get("candidates") or []
                if not choices:
                    send("当前私聊没有可核验的历史任务。" if history else
                         "当前私聊没有待处理的原任务。也可回复以前的机器人消息并发送“继续”查看原任务状态。")
                    return
                if history or len(choices) > 1 or verb == "stop":
                    shown = choices[:20]
                    lines = []
                    for i, task in enumerate(shown, 1):
                        title = public_mirror_text(task.get("title")) or "办公任务"
                        state = _STATES.get(task.get("status"), "待核对")
                        lines.append(f"{i}. {' '.join(title.split())[:60]} · {state}{_local_time(task.get('createdAt'))}")
                    action = "停止" if verb == "stop" else "继续查看"
                    prompt = ("无法核实这条旧通知的原任务。请选择要查看的本人历史任务" if history else f"请选择要{action}的任务")
                    send(f"{prompt}（列出 {len(shown)} 项）。回复本条消息并输入编号。\n" + "\n".join(lines),
                         target={"kind": "history_choices" if history else "choices", "action": verb, "ids": [task["taskId"] for task in shown],
                                 "expires": time.time() + 300})
                    return
                target = {"kind": "task", "id": choices[0]["taskId"]}
            if result is None:
                result = pilot.gateway._request("telegram-task", {**request, "operation": verb, "target": target})
        except GatewayRequestError:
            send("暂时无法核对原任务，请在本人工作台查看最新状态。")
            return
        if result.get("taskId") and target["kind"] not in {"result", "history_task", "status_delivery"}:
            target = {"kind": "task", "id": result["taskId"]}
        params = None
        if card := result.get("interaction"):
            parsed = urlsplit(str((card.get("presentation") or {}).get("url", "")))
            params = render_interaction_message(card, frozenset({f"https://{parsed.netloc}"}))
        elif view := result.get("resultView"):
            params = render_result_view_message(view)
            target = {"kind": "result", "id": view["sourceTaskId"]}
        if params:
            send(params["text"], target=target, formatted=params, native_source=result.get("nativeCardSource"))
            return
        state = result.get("state")
        if state in {"stop_unconfirmed", "unavailable"}:
            text = ("中央未确认任务可以停止，请查看原任务当前状态。" if state == "stop_unconfirmed"
                    else "未找到仍可打开的原任务或原卡。")
        else:
            text = "原任务状态：" + _STATES.get(state, "待核对") + "。"
            if state == "waiting_user":
                text += "当前没有可打开的原卡，请在本人工作台核对。"
        send(text, target=target)
