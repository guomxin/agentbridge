"""Pin the private Pi Telegram consumer to the approved subject and digital sender."""

import json
import re

from .private_files import private_text


def private_sender_policy(filename, config):
    allowed = config.get("allowedSenders")
    if (config.get("enabled") is not True or config.get("preparationOnly") is not False
            or not isinstance(allowed, dict) or len(allowed) != 1 or config.get("bindings") != {}
            or config.get("allowWorkspacePairing") is not True):
        raise ValueError("Pi Telegram requires an enabled single-subject pairing policy")
    subject, sender = next(iter(allowed.items()))
    if (not isinstance(subject, str) or not subject.strip() or not isinstance(sender, str)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", sender) or int(sender) >= 2**53):
        raise ValueError("Invalid Pi Telegram subject or digital sender")

    def permitted(chat):
        if type(chat) is not int or chat != int(sender):
            return None
        try:
            current = json.loads(private_text(filename))
        except (OSError, ValueError):
            return None
        # A changed identity, credential, consumer setting or allowlist requires
        # a reviewed restart. In-flight updates must not cross that boundary.
        return subject if current == config else None

    return permitted
