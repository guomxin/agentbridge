"""Choose a state directory without silently abandoning existing state."""
from pathlib import Path


def resolve_home(explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    legacy = Path.home() / ".bscli"
    current = Path.home() / ".agentbridge"
    # Even an unrecognized old directory needs an explicit operator decision.
    # A new directory containing only PKI does not prove business data migrated.
    if legacy.exists() or legacy.is_symlink():
        raise ValueError("Legacy ~/.bscli state exists. Pass --home explicitly to use "
                         "the reviewed state directory; see the data migration guide. "
                         "No state was created or moved.")
    return current
