"""Read-only counterpart check for an already authenticated NanoBot Telegram user."""

from .telegram_pilot_links import TelegramPilotLinks


def verified_sender(service, *, subject, instance, evidence):
    if not isinstance(evidence, dict) or set(evidence) != {"senderId", "botId"}:
        return False
    if not all(isinstance(value, str) for value in evidence.values()):
        return False
    try:
        return TelegramPilotLinks(service).verified(
            subject, instance, evidence["botId"], evidence["senderId"]
        )
    except (TypeError, ValueError):
        return False
