"""Verify a private sender against the central subject's existing Telegram identity."""

import re
from uuid import UUID


def identifier(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


async def verify_telegram_binding(
    session, instance, subject, host_context, endpoint_key, *, host_type="nanobot"
):
    match = re.fullmatch(
        r"telegram:([1-9][0-9]{0,15}):([1-9][0-9]{0,15})", endpoint_key
    )
    if not match or any(int(value) >= 2**53 for value in match.groups()):
        return False
    context = host_context.get("io.agentbridge/host-context", host_context)
    if (
        host_type not in {"nanobot", "pi"} or context.get("agentHost") != host_type
        or context.get("hostInstanceId") != instance
    ):
        return False
    result = await session.call_tool(
        "agentbridge_host_identity_profile",
        {"agent_host": host_type},
        meta={
            "io.agentbridge/host-context": context,
            "io.agentbridge/telegram-identity": {
                "botId": match[1],
                "senderId": match[2],
            },
        },
    )
    profile = result.structuredContent or {}
    return (
        not result.isError
        and profile.get("identity", {}).get("userSubject") == subject
        and profile.get("host", {}).get("hostInstanceId") == instance
        and profile.get("telegramIdentityVerified") is True
    )
