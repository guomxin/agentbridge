"""Resolve an independently verified private Telegram sender through named host records."""

import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from .telegram_identity import verify_telegram_binding


class TelegramRoutes:
    def __init__(self, host, spec):
        self.host = host
        self.bot = None
        self.web = {}
        self.known = {}
        options = spec.get("telegramPilot")
        if options is None:
            return
        self.bot = str(options["botId"])
        if (
            not re.fullmatch(r"[1-9][0-9]{0,15}", self.bot)
            or int(self.bot) >= 2**53
            or not isinstance(options.get("username"), str)
        ):
            raise ValueError("Invalid private Telegram pilot identity")
        for record in host.bindings.values():
            if record["endpoint_key"].startswith("telegram:"):
                continue
            subject = record["user_subject"]
            if subject in self.web:
                raise ValueError(
                    "One authorized Web record per Telegram pilot subject is required"
                )
            self.web[subject] = record

    def record(self, web, sender):
        basis = json.dumps(
            [
                self.host.instance,
                web["user_subject"],
                web["endpoint_id"],
                self.bot,
                sender,
            ],
            separators=(",", ":"),
        )
        uid = str(uuid5(NAMESPACE_URL, basis))
        credential = hashlib.sha256(
            (basis + "|" + web["credential_ref"]).encode()
        ).hexdigest()
        return {
            **web,
            "endpoint_id": uid,
            "endpoint_key": f"telegram:{self.bot}:{sender}",
            "session_key": "agentbridge:telegram:" + uid,
            "credential_ref": "tg-" + credential,
        }

    def get(self, key):
        if not self.bot or key[0] not in self.web:
            return None
        prefix = f"telegram:{self.bot}:"
        sender = key[2].removeprefix(prefix)
        if (
            not key[2].startswith(prefix)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", sender)
            or int(sender) >= 2**53
        ):
            return None
        return self.known.get(key)

    async def mirror_peers(self, body):
        import asyncio
        from aiohttp import web
        from .server import identity_denial
        from .session_pool import open_host_session

        if not self.bot or self.bot != body.get("botId"):
            raise web.HTTPForbidden()
        records = {**self.host.enrollment.identities, **self.web}

        async def fetch_subject(record):
            try:
                async with open_host_session(
                    self.host, self.host.worker_payload(record, None), lane="control"
                ) as (session, meta, level):
                    if level != "L3":
                        return []
                    result = await session.call_tool(
                        "agentbridge_host_telegram_mirror_peers",
                        {"bot_id": self.bot},
                        meta=meta,
                    )
                if result.isError:
                    return []
                peers = (result.structuredContent or {}).get("senderIds")
                return peers if isinstance(peers, list) else []
            except Exception as error:
                if not identity_denial(error):
                    raise
                return []

        results = await asyncio.gather(*(fetch_subject(record) for record in records.values()))
        senders = set()
        duplicates = set()
        for peers in results:
            for sender in peers:
                if not isinstance(sender, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", sender) or int(sender) >= 2**53:
                    continue
                if sender in senders:
                    duplicates.add(sender)
                senders.add(sender)
        return {"senderIds": sorted(senders - duplicates)}

    async def action(self, action, body):
        import asyncio
        from aiohttp import web

        from .server import BINDING_KEYS, identity_denial
        from .session_pool import open_host_session

        sender = body.get("senderId")
        if (
            body.get("botId") != self.bot
            or self.bot is None
            or not isinstance(sender, str)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", sender)
            or int(sender) >= 2**53
        ):
            raise web.HTTPForbidden()
        code = body.get("code")
        if action == "telegram-present" and (
            not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32}", code)
        ):
            raise web.HTTPBadRequest()

        async def probe_subject(subject, web_record):
            record = self.record(web_record, sender)
            try:
                async with open_host_session(
                    self.host, self.host.worker_payload(web_record, None), lane="control"
                ) as (session, meta, _):
                    if action == "telegram-route":
                        if await verify_telegram_binding(
                            session,
                            self.host.instance,
                            subject,
                            meta,
                            record["endpoint_key"],
                        ):
                            result = await session.call_tool(
                                "agentbridge_host_telegram_endpoint",
                                {"bot_id": self.bot, "sender_id": sender},
                                meta=meta,
                            )
                            paired = result.structuredContent or {}
                            if (
                                result.isError
                                or paired.get("endpointKey") != record["endpoint_key"]
                                or paired.get("conversationRef")
                                != record["session_key"]
                                or paired.get("workspaceEndpointId")
                                != web_record["endpoint_id"]
                                or paired.get("workspaceEndpointKey")
                                != web_record["endpoint_key"]
                                or not isinstance(paired.get("endpointId"), str)
                            ):
                                return None
                            record["endpoint_id"] = paired["endpointId"]
                            record.update(
                                workspaceEndpointId=paired["workspaceEndpointId"],
                                workspaceEndpointKey=paired["workspaceEndpointKey"],
                            )
                            return record
                        return None
                    response = await session.call_tool(
                        "agentbridge_host_telegram_pair_present",
                        {"bot_id": self.bot, "sender_id": sender, "code": code},
                        meta=meta,
                    )
                    if response.isError:
                        return None
                    proof = (response.meta or {}).get(
                        "io.agentbridge/telegram-confirmation"
                    )
                    if isinstance(proof, str) and re.fullmatch(r"[0-9]{8}", proof):
                        return proof
                    return None
            except Exception as error:
                if not identity_denial(error):
                    raise
                return None

        outcomes = await asyncio.gather(
            *(probe_subject(subject, web_record) for subject, web_record in self.web.items())
        )
        if action == "telegram-route":
            matches = [item for item in outcomes if isinstance(item, dict)]
            if len(matches) != 1:
                return {"binding": None}
            from .server import binding_key

            record = matches[0]
            self.known[binding_key(record)] = record
            return {"binding": {key: record[key] for key in BINDING_KEYS}}
        for proof in outcomes:
            if isinstance(proof, str):
                return {"confirmationCode": proof}
        return {"confirmationCode": None}
