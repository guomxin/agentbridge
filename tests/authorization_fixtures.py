"""Explicit user grants for business-behavior tests, separate from identity issuance."""
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.user_grants import PERMISSIONS


def grant_permissions(grants, subject, permissions):
    current = grants.get(subject)
    return grants.save(subject, sorted(permissions), expected_revision=current["revision"] if current else 0,
                       actor="test-admin", reason="Explicit test business authorization")


def authorized_service(*args, **kwargs):
    service = CentralCapabilityService(*args, **kwargs)
    for subject in ("user-a", "user-b"):
        grant_permissions(service.user_grants, subject, PERMISSIONS)
    return service


def issue_authorized_token(store, *, scopes=None, **kwargs):
    """Translate old test setup into a saved user grant; the token itself has no rights."""
    requested = set(scopes if scopes is not None else ["oa:read"])
    permissions = [p for p, spec in PERMISSIONS.items()
                   if spec["legacy_scopes"] and set(spec["legacy_scopes"]).issubset(requested)]
    grant_permissions(store.user_grants, kwargs["user_subject"], permissions)
    return store.issue(**kwargs)
