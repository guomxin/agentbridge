from __future__ import annotations


class AdapterLoginRequired(RuntimeError):
    """The downstream session is absent or can no longer be refreshed."""


class AdapterAuthenticationRejected(RuntimeError):
    """The downstream system rejected the submitted credentials."""


class AdapterLoginContractMismatch(RuntimeError):
    """The registered authentication contract no longer matches the system."""


class AdapterUnsupportedAuthMethod(RuntimeError):
    """The login requires a flow that the trusted credential card cannot complete."""


class AdapterSessionCheckUnavailable(RuntimeError):
    """Session validity could not be determined without discarding stored state."""


class AdapterBusinessRuleRejected(RuntimeError):
    error_code = "BUSINESS_RULE_REJECTED"
