from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Mapping


@dataclass(frozen=True)
class IdentityConfig:
    label: str
    token_env: str
    endpoint_key: str
    external_subject: str
    conversation_ref: str
    expected_subject: str | None = None
    expected_endpoint: str | None = None
    account_id: str | None = None

    def token(self) -> str:
        value = os.environ.get(self.token_env, "").strip()
        if not value:
            raise RuntimeError(
                f"MCP bearer environment variable is unavailable: {self.token_env}"
            )
        return value

    def public_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "tokenEnv": self.token_env,
            "endpointKey": self.endpoint_key,
            "externalSubject": self.external_subject,
            "conversationRef": self.conversation_ref,
        }


class IdentityRegistry:
    def __init__(self, identities: list[IdentityConfig]) -> None:
        if not identities:
            raise ValueError("at least one reference-host identity is required")
        self._by_label = {identity.label: identity for identity in identities}
        if len(self._by_label) != len(identities):
            raise ValueError("reference-host identity labels must be unique")
        endpoint_keys = {identity.endpoint_key for identity in identities}
        if len(endpoint_keys) != len(identities):
            raise ValueError("reference-host endpoint keys must be unique")

    @classmethod
    def from_environment(cls) -> "IdentityRegistry":
        raw = os.environ.get("AGENTBRIDGE_REFERENCE_IDENTITIES_JSON", "").strip()
        if raw:
            try:
                values = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "AGENTBRIDGE_REFERENCE_IDENTITIES_JSON is invalid"
                ) from exc
            if not isinstance(values, list):
                raise ValueError("reference-host identities must be a JSON array")
            return cls([_identity_from_mapping(item) for item in values])

        label = os.environ.get("AGENTBRIDGE_REFERENCE_IDENTITY_LABEL", "default")
        token_env = os.environ.get(
            "AGENTBRIDGE_REFERENCE_TOKEN_ENV",
            "AGENTBRIDGE_MCP_TOKEN",
        )
        slug = _slug(label)
        return cls(
            [
                IdentityConfig(
                    label=_text(label, "label", 120),
                    token_env=_token_env(token_env),
                    endpoint_key=f"reference-host:{slug}",
                    external_subject=f"reference-host:{slug}",
                    conversation_ref=f"reference-host:conversation:{slug}",
                )
            ]
        )

    def get(self, label: str) -> IdentityConfig:
        try:
            return self._by_label[label]
        except KeyError as exc:
            raise KeyError(f"reference-host identity not found: {label}") from exc

    def list(self) -> list[IdentityConfig]:
        return list(self._by_label.values())


def _identity_from_mapping(value: Any) -> IdentityConfig:
    if not isinstance(value, Mapping):
        raise ValueError("reference-host identity must be an object")
    label = _text(value.get("label"), "label", 120)
    slug = _slug(label)
    return IdentityConfig(
        label=label,
        token_env=_token_env(value.get("tokenEnv")),
        endpoint_key=_text(
            value.get("endpointKey") or f"reference-host:{slug}",
            "endpointKey",
            768,
        ),
        external_subject=_text(
            value.get("externalSubject") or f"reference-host:{slug}",
            "externalSubject",
            768,
        ),
        conversation_ref=_text(
            value.get("conversationRef")
            or f"reference-host:conversation:{slug}",
            "conversationRef",
            1024,
        ),
    )


def _token_env(value: Any) -> str:
    normalized = _text(value, "tokenEnv", 160)
    if not normalized.replace("_", "A").isalnum() or normalized[0].isdigit():
        raise ValueError("tokenEnv is invalid")
    return normalized


def _slug(value: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-"
        for character in value
    ).strip("-")
    return normalized[:80] or "identity"


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, (str, int)):
        raise ValueError(f"{name} is required")
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized
