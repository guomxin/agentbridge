from __future__ import annotations

import logging
import threading
from typing import Callable

from agentbridge.adapters.base import (
    AdapterAuthenticationRejected,
    AdapterLoginContractMismatch,
    AdapterSessionCheckUnavailable,
    AdapterUnsupportedAuthMethod,
)
from agentbridge.browser.central import CentralProfileUnavailableError
from agentbridge.core.auth_challenges import AuthChallengeStore
from agentbridge.core.session_secrets import SessionSecretError, SessionStateStore
from agentbridge.core.sessions import SessionPrincipalMismatch, SessionRegistry


logger = logging.getLogger(__name__)


class CredentialBroker:
    def __init__(
        self,
        *,
        challenge_store: AuthChallengeStore,
        session_registry: SessionRegistry,
        session_state_store: SessionStateStore,
        adapter_factory: Callable[[dict], object],
        worker_factory: Callable[[dict, object], object],
        login_timeout_seconds: float = 45,
    ) -> None:
        self.challenge_store = challenge_store
        self.session_registry = session_registry
        self.session_state_store = session_state_store
        self.adapter_factory = adapter_factory
        self.worker_factory = worker_factory
        self.login_timeout_seconds = login_timeout_seconds
        self._preparation_lock = threading.Lock()

    def prepare_authentication(self, *, challenge_id: str) -> dict | None:
        challenge = self.challenge_store.get(challenge_id)
        session = self.session_registry.get(challenge["session_id"])
        adapter = self.adapter_factory(challenge)
        contract = adapter.authentication_contract()
        self._validate_bindings(challenge, session, contract)
        prepare = getattr(adapter, "prepare_authentication", None)
        if not callable(prepare):
            return None

        with self._preparation_lock:
            with self.worker_factory(session, adapter) as worker:
                prepared = None
                recover = getattr(adapter, "recover_prepared_authentication", None)
                existing_state = self.session_state_store.load(session["session_id"])
                if existing_state is not None and callable(recover):
                    try:
                        worker.restore_session_state(existing_state)
                        prepared = recover(worker)
                    except (KeyError, TypeError, ValueError):
                        prepared = None
                if prepared is None:
                    worker.clear_session_state()
                    prepared = prepare(
                        worker,
                        timeout_seconds=self.login_timeout_seconds,
                    )
                    self.session_state_store.save(
                        session["session_id"],
                        worker.capture_session_state(),
                    )
            self.session_registry.mark_awaiting_login(
                session["session_id"],
                source="credential_challenge",
            )
        return prepared

    def authenticate(
        self,
        *,
        challenge_id: str,
        csrf_token: str,
        csrf_cookie: str,
        credentials: dict,
    ) -> dict:
        try:
            challenge = self.challenge_store.claim(
                challenge_id,
                csrf_token=csrf_token,
                csrf_cookie=csrf_cookie,
            )
        except Exception:
            credentials.clear()
            raise

        session = None
        try:
            session = self.session_registry.get(challenge["session_id"])
            adapter = self.adapter_factory(challenge)
            contract = adapter.authentication_contract()
            self._validate_bindings(challenge, session, contract)
            self._validate_credentials(challenge["fields"], credentials)
            self.session_registry.mark_awaiting_login(
                session["session_id"],
                source="credential_challenge",
            )

            with self.worker_factory(session, adapter) as worker:
                if callable(getattr(adapter, "prepare_authentication", None)):
                    prepared_state = self.session_state_store.load(
                        session["session_id"]
                    )
                    if prepared_state is None:
                        raise AdapterLoginContractMismatch(
                            "The prepared authentication state is missing."
                        )
                    worker.restore_session_state(prepared_state)
                else:
                    self.session_state_store.delete(session["session_id"])
                    worker.clear_session_state()
                authentication = adapter.authenticate(
                    worker,
                    credentials,
                    timeout_seconds=self.login_timeout_seconds,
                )
                active_session = self.session_registry.activate(
                    session["session_id"],
                    observed_principal_ref=authentication.get("observed_principal_ref"),
                    source="credential_login",
                )
                self.session_state_store.save(
                    session["session_id"],
                    worker.capture_session_state(),
                )

            templates = authentication.get("templates")
            result = {
                "status": "succeeded",
                "challengeId": challenge_id,
                "sessionId": active_session["session_id"],
                "systemId": active_session["system_id"],
                "observedPrincipalRef": active_session["downstream_principal_ref"],
                "templateCount": templates.get("count") if isinstance(templates, dict) else None,
            }
            self.challenge_store.complete(
                challenge_id,
                result={
                    "session_id": active_session["session_id"],
                    "observed_principal_ref": active_session["downstream_principal_ref"],
                    "template_count": result["templateCount"],
                },
            )
            return result
        except SessionPrincipalMismatch:
            return self._fail(
                challenge_id,
                session,
                code="PRINCIPAL_MISMATCH",
                message="The authenticated downstream identity did not match the expected identity.",
                quarantine=True,
            )
        except AdapterUnsupportedAuthMethod:
            return self._fail(
                challenge_id,
                session,
                code="UNSUPPORTED_AUTH_METHOD",
                message="This login requires an authentication method not supported by the card.",
            )
        except AdapterLoginContractMismatch:
            return self._fail(
                challenge_id,
                session,
                code="LOGIN_CONTRACT_MISMATCH",
                message="The login no longer matches its registered contract.",
            )
        except AdapterAuthenticationRejected as exc:
            rejection_code = str(
                getattr(exc, "error_code", "AUTHENTICATION_REJECTED")
            )
            messages = {
                "AUTHENTICATION_REJECTED": (
                    "The downstream system did not accept the submitted "
                    "authentication information."
                ),
                "CAPTCHA_REJECTED": (
                    "The downstream system did not accept the verification code."
                ),
                "CREDENTIALS_REJECTED": (
                    "The downstream system did not accept the account or password."
                ),
            }
            if rejection_code not in messages:
                rejection_code = "AUTHENTICATION_REJECTED"
            return self._fail(
                challenge_id,
                session,
                code=rejection_code,
                message=messages[rejection_code],
            )
        except (AdapterSessionCheckUnavailable, ConnectionError):
            return self._fail(
                challenge_id,
                session,
                code="DOWNSTREAM_UNAVAILABLE",
                message="The downstream system is temporarily unavailable; please try login again later.",
            )
        except SessionSecretError:
            return self._fail(
                challenge_id,
                session,
                code="SESSION_STATE_UNAVAILABLE",
                message="The encrypted downstream session could not be saved.",
            )
        except CentralProfileUnavailableError:
            return self._fail(
                challenge_id,
                session,
                code="SESSION_PROFILE_UNAVAILABLE",
                message="The managed downstream session worker is not writable.",
            )
        except (KeyError, TypeError, ValueError):
            return self._fail(
                challenge_id,
                session,
                code="AUTHENTICATION_REQUEST_INVALID",
                message="The authentication request did not match the registered contract.",
            )
        except Exception:
            logger.exception(
                "Credential Broker failed unexpectedly for challenge %s and session %s",
                challenge_id,
                session.get("session_id") if session else None,
            )
            return self._fail(
                challenge_id,
                session,
                code="BROKER_LOGIN_FAILED",
                message="The credential broker could not complete the downstream login.",
            )
        finally:
            credentials.clear()

    def _fail(
        self,
        challenge_id: str,
        session: dict | None,
        *,
        code: str,
        message: str,
        quarantine: bool = False,
    ) -> dict:
        if session is not None:
            try:
                self.session_state_store.delete(session["session_id"])
            except SessionSecretError:
                pass
            if quarantine:
                current = self.session_registry.get(session["session_id"])
                if current["state"] != "quarantined":
                    self.session_registry.quarantine(
                        session["session_id"],
                        message,
                        source="credential_login",
                    )
            else:
                self.session_registry.mark_expired(
                    session["session_id"],
                    message,
                    source="credential_login",
                )
        challenge = self.challenge_store.fail(challenge_id, code=code, message=message)
        return {
            "status": "failed",
            "challengeId": challenge_id,
            "error": challenge["error"],
        }

    @staticmethod
    def _validate_bindings(challenge: dict, session: dict, contract: dict) -> None:
        if session["session_id"] != challenge["session_id"]:
            raise ValueError("challenge session mismatch")
        if session["user_subject"] != challenge["user_subject"]:
            raise ValueError("challenge user mismatch")
        if session["system_id"] != challenge["system_id"]:
            raise ValueError("challenge system mismatch")
        if session.get("expected_principal_ref") != challenge.get("expected_principal_ref"):
            raise ValueError("challenge principal mismatch")
        if contract.get("system_id") != challenge["system_id"]:
            raise ValueError("authentication contract system mismatch")
        if contract.get("origin") != challenge["origin"]:
            raise ValueError("authentication contract origin mismatch")
        if contract.get("page_fingerprint") != challenge["page_fingerprint"]:
            raise ValueError("authentication contract fingerprint mismatch")
        if contract.get("fields") != challenge["fields"]:
            raise ValueError("authentication contract fields mismatch")

    @staticmethod
    def _validate_credentials(fields: list[dict], credentials: dict) -> None:
        allowed = {field["name"] for field in fields}
        required = {field["name"] for field in fields if field.get("required")}
        if set(credentials) != allowed:
            raise ValueError("authentication fields mismatch")
        if any(name not in credentials or not credentials[name] for name in required):
            raise ValueError("required authentication field is missing")
        for name, value in credentials.items():
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError(f"invalid authentication field: {name}")
