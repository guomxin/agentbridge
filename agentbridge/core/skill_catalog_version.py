"""Opaque cache validators for instructions and their current dependencies."""
from hashlib import sha256
import json


def _stamp(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(raw.encode()).hexdigest()


def skill_catalog_stamps(service, subject, *, catalog=None):
    if catalog is None:
        from agentbridge.core.business_skills import skill_catalog
        catalog = skill_catalog(service, subject)
    grant = service.user_grants.get(subject) or {}
    allowed = sorted(service.user_grants.available_tools(subject))
    assigned = [{key: item.get(key) for key in ("id", "version", "content_hash", "status", "assigned", "settings", "profiles")}
                for item in catalog.get("items", []) if item.get("assigned")]
    return {
        "catalogVersion": _stamp({"revision": catalog.get("revision"), "globalRevision": catalog.get("global_revision"),
            "items": sorted(assigned, key=lambda item: item["id"])}),
        "permissionVersion": _stamp({"revision": grant.get("revision", 0), "allowedTools": allowed}),
    }
