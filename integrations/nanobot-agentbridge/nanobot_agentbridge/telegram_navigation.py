"""Private-chat command and confirmed-receipt routing for existing Telegram tasks."""

import json
import re
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from agentbridge.core.chat_mirror import public_mirror_text
from agentbridge.workspace.gateway import GatewayRequestError

from .telegram_presentation import render_interaction_message, render_result_view_message


_COMMANDS = {
    "/pending": "list", "/agentbridge pending": "list", "找回原卡": "list",
    "找回卡片": "list", "查看待处理任务": "list",
    "/continue": "continue", "继续": "continue", "继续办理": "continue", "继续原任务": "continue",
    "/stop": "stop", "/cancel": "stop", "停止": "stop", "停止任务": "stop", "取消任务": "stop",
}
_STATES = {
    "waiting_user": "等待本人操作", "active": "进行中", "running": "执行中",
    "succeeded": "已完成", "failed": "未完成", "canceled": "已停止",
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
        if (not isinstance(reply, dict) or (reply.get("from") or {}).get("id") != pilot.bot_id
                or type(reply.get("message_id")) is not int
                or ("chat" in reply and (reply["chat"].get("type"), reply["chat"].get("id")) != ("private", chat))
                or any(k in reply for k in ("forward_origin", "sender_chat", "via_bot"))):
            return None
        key = (pilot.bot_id, chat, reply["message_id"])
        saved = pilot.db.execute(
            "SELECT digest,target_json FROM telegram_navigation_receipts WHERE bot=? AND chat=? AND receipt=?", key
        ).fetchone()
        if saved:
            return json.loads(saved[1]) if saved[0] == digest else None
        # Older ordinary replies already have the binding digest and exact original run.
        rows = pilot.db.execute("""SELECT DISTINCT m.message,m.run_id FROM messages m
            LEFT JOIN telegram_reply_parts p ON (p.bot,p.chat,p.message)=(m.bot,m.chat,m.message)
            WHERE m.bot=? AND m.chat=? AND (m.receipt=? OR p.receipt=?) AND m.binding_hash=? AND m.run_id IS NOT NULL""",
            (*key, key[-1], digest)).fetchall()
        if len(rows) == 1:
            return {"kind": "run", "id": rows[0][1], "requestKey": pilot._key(chat, rows[0][0])}
        # Previously delivered cards have opaque IDs only. The host verifies original
        # endpoint, task identity, current binding and original conversation before use.
        cards = pilot.db.execute("""SELECT dedupe_key FROM telegram_card_deliveries
            WHERE bot=? AND chat=? AND receipt=? AND state='delivered'""", key).fetchall()
        if len(cards) == 1:
            prefix, _, ident = cards[0][0].partition(":")
            if prefix in {"interaction", "result_view"}:
                return {"kind": "interaction" if prefix == "interaction" else "result", "id": ident}
        return None

    def handle(self, chat, message, binding, digest, reply, verb, ordinal):
        pilot = self.pilot

        def send(text, *, target=None, formatted=None):
            active, proof = pilot._binding(chat)
            if active != binding or proof != digest:
                pilot._state(chat, message, "binding_changed")
                return
            pilot._state(chat, message, "sending")
            params = formatted or {}
            receipt = pilot._confirmed_send(chat, message, text, parse_mode=params.get("parse_mode"),
                                            reply_markup=params.get("reply_markup"))
            with pilot.db:
                self.remember(chat, receipt, digest, target)
                pilot.db.execute("UPDATE messages SET state=?, receipt=? WHERE bot=? AND chat=? AND message=?",
                                 ("delivered" if receipt else "delivery_unknown", receipt, pilot.bot_id, chat, message))

        target = self.quoted(chat, digest, reply) if reply is not None else None
        if reply is not None and not target:
            send("无法核对这条机器人消息的原任务。发送 /pending 查看本人当前任务。")
            return
        if target and target["kind"] == "choices":
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
            if not target:
                reply_data = pilot.gateway._request("telegram-task", {**request, "operation": "list"})
                choices = reply_data.get("candidates") or []
                if not choices:
                    send("当前私聊没有待处理的原任务。也可回复以前的机器人消息并发送“继续”查看原任务状态。")
                    return
                if len(choices) > 1 or verb == "stop":
                    shown = choices[:20]
                    lines = []
                    for i, task in enumerate(shown, 1):
                        title = public_mirror_text(task.get("title")) or "办公任务"
                        state = _STATES.get(task.get("status"), "待核对")
                        lines.append(f"{i}. {' '.join(title.split())[:60]} · {state}{_local_time(task.get('createdAt'))}")
                    action = "停止" if verb == "stop" else "继续查看"
                    send(f"请选择要{action}的任务（列出 {len(shown)} 项）。回复本条消息并输入编号。\n" + "\n".join(lines),
                         target={"kind": "choices", "action": verb, "ids": [task["taskId"] for task in shown],
                                 "expires": time.time() + 300})
                    return
                target = {"kind": "task", "id": choices[0]["taskId"]}
            result = pilot.gateway._request("telegram-task", {**request, "operation": verb, "target": target})
        except GatewayRequestError:
            send("暂时无法核对原任务，请在本人工作台查看最新状态。")
            return
        if result.get("taskId") and target["kind"] != "result":
            target = {"kind": "task", "id": result["taskId"]}
        params = None
        if card := result.get("interaction"):
            parsed = urlsplit(str((card.get("presentation") or {}).get("url", "")))
            params = render_interaction_message(card, frozenset({f"https://{parsed.netloc}"}))
        elif view := result.get("resultView"):
            params = render_result_view_message(view)
            target = {"kind": "result", "id": view["sourceTaskId"]}
        if params:
            send(params["text"], target=target, formatted=params)
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
