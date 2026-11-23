"""Bootstrap previously assigned subjects without an existing Workspace login."""

import re

from .worker import authenticated_session


class Enrollment:
    def __init__(self, host, assignments):
        self.host = host
        self.bot = host.telegram_routes.bot
        self.identities = {}
        self.senders = {}
        for subject, sender in assignments.items():
            if (not self.bot or not isinstance(sender, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", sender)
                    or int(sender) >= 2**53 or sender in self.senders):
                raise ValueError("Invalid or shared enrollment sender")
            endpoint = f"telegram:{self.bot}:{sender}"
            keys = [key for key in host.bindings if key[0] == subject and key[2] == endpoint]
            if len(keys) != 1 or subject in host.telegram_routes.web:
                raise ValueError("Expected one isolated pre-enrollment identity")
            self.identities[subject] = host.bindings.pop(keys[0])
            self.senders[sender] = subject

    async def confirm(self, bot, sender, code):
        if bot != self.bot or sender not in self.senders or not re.fullmatch(r"[A-Z2-9]{8}", code):
            return False
        record = self.identities[self.senders[sender]]
        async with authenticated_session(self.host.worker_payload(record, None)) as (session, meta, _):
            result = await session.call_tool("agentbridge_host_workspace_link_confirm", {
                "agent_host": "nanobot", "client_type": "telegram", "endpoint_key": record["endpoint_key"],
                "external_subject": sender, "account_id": bot, "conversation_ref": record["session_key"],
                "link_code": code,
            }, meta=meta)
            return not result.isError and (result.structuredContent or {}).get("link", {}).get("state") == "confirmed"

    async def web_record(self, sender):
        from .server import binding_key
        subject = self.senders.get(sender)
        if not subject:
            return None
        seed = self.identities[subject]
        async with authenticated_session(self.host.worker_payload(seed, None)) as (session, meta, _):
            response = await session.call_tool("agentbridge_host_enrolled_workspace_binding",
                                               {"bot_id": self.bot, "sender_id": sender}, meta=meta)
        binding = (response.meta or {}).get("io.agentbridge/workspace-binding")
        if response.isError or not isinstance(binding, dict):
            return None
        key = binding_key(binding)
        if (key[0] != subject or key[4] != seed["credential_ref"]
                or not key[2].startswith(f"workspace:{self.host.instance}:") or key[3] != key[2]):
            return None
        web = {**seed, **binding}
        self.host.telegram_routes.web[subject] = web
        return web

    async def resolve_web(self, key):
        from .server import binding_key
        source = self.identities.get(key[0])
        if not source or source["credential_ref"] != key[4] or not key[2].startswith("workspace:"):
            return None
        record = await self.web_record(source["endpoint_key"].rsplit(":", 1)[1])
        return record if record and binding_key(record) == key else None
