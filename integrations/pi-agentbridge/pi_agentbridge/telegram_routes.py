"""Resolve the single approved Pi private sender using the web owner's central credential."""

import re
from uuid import UUID

from starlette.exceptions import HTTPException

from agentbridge.workspace.telegram_pilot_links import telegram_conversation_ref



def digital(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,15}", value)) and int(value) < 2**53


class PiTelegramRoutes:
    def __init__(self, host):
        self.host = host
        self.web = self.sender = self.bot = None
        pilot = host.spec.get("telegramPilot")
        if pilot is None:
            return
        self.bot = str(pilot["botId"])
        allowed = pilot.get("allowedSenders")
        if not digital(self.bot) or not isinstance(allowed, dict) or len(allowed) != 1:
            raise ValueError("Pi Telegram requires exactly one reviewed sender")
        subject, self.sender = next(iter(allowed.items()))
        matches = [b for b in host.bindings.values() if b["user_subject"] == subject]
        if not digital(self.sender) or len(matches) != 1:
            raise ValueError("Pi Telegram requires its own subject's exact web identity")
        self.web = matches[0]

    async def action(self, action, body):
        if not self.web or body.get("botId") != self.bot:
            raise HTTPException(403)
        if action != "telegram-mirror-peers" and body.get("senderId") != self.sender:
            raise HTTPException(403)
        from .server import BINDING_FIELDS

        async with self.host.control_sessions.borrow(self.web) as central:
            ids = {"bot_id": self.bot, "sender_id": self.sender}
            if action == "telegram-present":
                code = body.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32}", code):
                    raise HTTPException(400)
                result = await central.session.call_tool("agentbridge_host_telegram_pair_present",
                                                         {**ids, "code": code}, meta=central.meta)
                proof = (result.meta or {}).get("io.agentbridge/telegram-confirmation")
                return {"confirmationCode": proof if not result.isError and isinstance(proof, str)
                        and re.fullmatch(r"[0-9]{8}", proof) else None}
            if action == "telegram-enroll":
                code = body.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z2-9]{8}", code):
                    raise HTTPException(400)
                endpoint = f"telegram:{self.bot}:{self.sender}"
                result = await central.session.call_tool("agentbridge_host_workspace_link_confirm", {
                    "agent_host": "pi", "client_type": "telegram", "endpoint_key": endpoint,
                    "external_subject": self.sender, "account_id": self.bot, "conversation_ref": endpoint,
                    "link_code": code}, meta=central.meta)
                return {"confirmed": not result.isError and (result.structuredContent or {}).get("link", {}).get("state") == "confirmed"}
            if action == "telegram-mirror-peers":
                if getattr(central, "level", "L1") != "L3":
                    return {"senderIds": []}
                result = await central.session.call_tool("agentbridge_host_telegram_mirror_peers",
                                                         {"bot_id": self.bot}, meta=central.meta)
                peers = (result.structuredContent or {}).get("senderIds")
                return {"senderIds": [self.sender] if not result.isError and peers == [self.sender] else []}
            if action != "telegram-route":
                raise HTTPException(404)
            record = await self.current(central)
            return {"binding": {k: record[k] for k in BINDING_FIELDS} if record else None}

    async def current(self, central):
        identity = await central.session.call_tool("agentbridge_host_identity_profile", {"agent_host": "pi"},
                   meta={**central.meta, "io.agentbridge/telegram-identity": {"botId": self.bot, "senderId": self.sender}})
        proof = identity.structuredContent or {}
        if (identity.isError or proof.get("telegramIdentityVerified") is not True
                or proof.get("identity", {}).get("userSubject") != self.web["user_subject"]
                or proof.get("host", {}).get("hostInstanceId") != self.host.instance):
            return None
        result = await central.session.call_tool("agentbridge_host_telegram_endpoint",
                  {"bot_id": self.bot, "sender_id": self.sender}, meta=central.meta)
        value = result.structuredContent or {}
        key = f"telegram:{self.bot}:{self.sender}"
        if (result.isError or value.get("endpointKey") != key or value.get("workspaceEndpointId") != self.web["endpoint_id"]
                or value.get("workspaceEndpointKey") != self.web["endpoint_key"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("pairingRef", "")))):
            return None
        try:
            endpoint = str(UUID(value["endpointId"]))
            conversation = value["conversationRef"]
            expected = telegram_conversation_ref(self.host.instance, self.web["user_subject"],
                                                 self.web["endpoint_id"], self.bot, self.sender)
            if conversation != expected:
                return None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return {**self.web, "endpoint_id": endpoint, "endpoint_key": key, "session_key": conversation,
                "credential_ref": "tg-" + value["pairingRef"], "workspaceEndpointId": self.web["endpoint_id"],
                "workspaceEndpointKey": self.web["endpoint_key"]}

    async def verified(self, key):
        if not self.web or key[0] != self.web["user_subject"] or key[2] != f"telegram:{self.bot}:{self.sender}":
            return None
        from .server import binding_key

        async with self.host.control_sessions.borrow(self.web) as central:
            record = await self.current(central)
            return record if record and binding_key(record) == key else None
