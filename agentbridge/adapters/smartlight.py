from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

from agentbridge.adapters.base import (
    AdapterAuthenticationRejected,
    AdapterBusinessRuleRejected,
    AdapterLoginContractMismatch,
    AdapterLoginRequired,
    AdapterSessionCheckUnavailable,
)
from agentbridge.core.capability import CapabilityRegistry, CapabilitySpec
from agentbridge.core.write_workflow import WriteWorkflowDefinition


SMARTLIGHT_SYSTEM_ID = "smartlight"
SMARTLIGHT_ADAPTER_ID = "smartlight-central"
SMARTLIGHT_SYSTEM_NAME = "照明实验室测试系统"

SMARTLIGHT_OVERVIEW_CAPABILITY = "smartlight.system.overview"
SMARTLIGHT_LAMPPOST_LIST_CAPABILITY = "smartlight.lamppost.list"
SMARTLIGHT_ALARM_LIST_CAPABILITY = "smartlight.alarm.list"
SMARTLIGHT_ALARM_REMARK_GET_CAPABILITY = "smartlight.alarm.remark.get"
SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY = "smartlight.inspection_task.list"
SMARTLIGHT_LEAKAGE_SUMMARY_CAPABILITY = "smartlight.leakage.summary"
SMARTLIGHT_ASSET_SEARCH_CAPABILITY = "smartlight.asset.search"
SMARTLIGHT_ASSET_DETAIL_CAPABILITY = "smartlight.asset.detail"
SMARTLIGHT_ALARM_ANALYSIS_CAPABILITY = "smartlight.alarm.analysis"
SMARTLIGHT_INSPECTION_TASK_DETAIL_CAPABILITY = "smartlight.inspection_task.detail"
SMARTLIGHT_LEAKAGE_ANALYSIS_CAPABILITY = "smartlight.leakage.analysis"
SMARTLIGHT_REPORT_EXPORT_CAPABILITY = "smartlight.report.export"
SMARTLIGHT_RUNTIME_OVERVIEW_CAPABILITY = "smartlight.runtime.overview"
SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY = "smartlight.rtu.status.list"
SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY = "smartlight.lamp.status.list"
SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY = "smartlight.lamp.alarm.list"
SMARTLIGHT_LAMP_ALARM_ANALYSIS_CAPABILITY = "smartlight.lamp.alarm.analysis"
SMARTLIGHT_RTU_SURVEY_RECORDS_CAPABILITY = "smartlight.rtu.survey.records"
SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY = "smartlight.energy_record.list"
SMARTLIGHT_ENERGY_ANALYSIS_CAPABILITY = "smartlight.energy.analysis"
SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY = "smartlight.lamp.survey.records"
SMARTLIGHT_RTU_LEAKAGE_ALARM_LIST_CAPABILITY = (
    "smartlight.rtu.leakage_alarm.list"
)
SMARTLIGHT_RTU_LEAKAGE_ANALYSIS_CAPABILITY = (
    "smartlight.rtu.leakage_alarm.analysis"
)
SMARTLIGHT_OFF_HOURS_CURRENT_LIST_CAPABILITY = (
    "smartlight.rtu.off_hours_current.list"
)
SMARTLIGHT_INSPECTION_LOG_LIST_CAPABILITY = "smartlight.inspection_log.list"
SMARTLIGHT_MAINTENANCE_RECORD_LIST_CAPABILITY = (
    "smartlight.maintenance_record.list"
)
SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY = (
    "smartlight.alarm.remark.update.prepare"
)
SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY = "smartlight.alarm.remark.update"
SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY = (
    "smartlight.alarm.work_area.submit.prepare"
)
SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_CAPABILITY = (
    "smartlight.alarm.work_area.submit"
)
SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY = (
    "smartlight.alarm.work_area.revoke.prepare"
)
SMARTLIGHT_ALARM_WORK_AREA_REVOKE_CAPABILITY = (
    "smartlight.alarm.work_area.revoke"
)
SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY = (
    "smartlight.alarm.dispose.prepare"
)
SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY = "smartlight.alarm.dispose"

SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alarm_id": {"type": "string"},
        "remark": {"type": "string"},
        "input_submission_id": {"type": "string"},
    },
    "required": ["alarm_id"],
    "additionalProperties": False,
}

SMARTLIGHT_ALARM_REMARK_UPDATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

SMARTLIGHT_ALARM_ACTION_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"alarm_id": {"type": "string"}},
    "required": ["alarm_id"],
    "additionalProperties": False,
}

SMARTLIGHT_ALARM_ACTION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.smartlight_alarm_remark_fields.v1",
    "title": "修改 RTU 告警备注",
    "system": SMARTLIGHT_SYSTEM_NAME,
    "effect": "修改一条 RTU 告警的备注；留空表示清除备注",
    "submit_label": "提交字段",
    "notice": "字段提交后还需单独授权；授权前不会修改照明系统。",
    "fields": [
        {
            "name": "remark",
            "label": "告警备注（留空表示清除）",
            "control": "textarea",
            "required": False,
            "max_length": 500,
            "rows": 5,
        }
    ],
}

_SMARTLIGHT_ANALYSIS_LIMIT = 500
_SMARTLIGHT_ANALYSIS_PAGE_SIZE = 100
_SMARTLIGHT_ALARM_TIE_PROBE_SIZE = 20
_SMARTLIGHT_RUNTIME_SCAN_LIMIT = 500
_SMARTLIGHT_SURVEY_MAX_DAYS = 7
_SMARTLIGHT_ENERGY_RECORD_MAX_DAYS = 92
_SMARTLIGHT_ENERGY_ANALYSIS_MAX_DAYS = 366
_SMARTLIGHT_RTU_LEAKAGE_MAX_DAYS = 366
_SMARTLIGHT_OPERATION_LOG_MAX_DAYS = 366
_SMARTLIGHT_ALARM_SORTS = {
    "occurred_at": ("occurredAt", "0"),
    "last_activity": ("lastActivityAt", "1"),
}

_SMARTLIGHT_REFRESH_LOGIN_REQUIRED_CODES = {"1009"}

_FORM_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

_CAS_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_SMARTLIGHT_BUSINESS_TIMEZONE_NAME = "Asia/Shanghai"
_SMARTLIGHT_BUSINESS_TIMEZONE = timezone(
    timedelta(hours=8),
    name=_SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
)


class SmartlightLoginRequired(AdapterLoginRequired):
    pass


class SmartlightAuthenticationRejected(AdapterAuthenticationRejected):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "AUTHENTICATION_REJECTED",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code


class SmartlightLoginContractMismatch(AdapterLoginContractMismatch):
    pass


class SmartlightSessionCheckUnavailable(AdapterSessionCheckUnavailable):
    pass


class SmartlightBusinessRuleRejected(AdapterBusinessRuleRejected):
    error_code = "SMARTLIGHT_BUSINESS_RULE_REJECTED"


class SmartlightAlarmRemarkContractMismatch(ValueError):
    pass


class SmartlightAlarmRemarkOutcomeUnknown(RuntimeError):
    pass


class SmartlightAlarmActionContractMismatch(ValueError):
    pass


class SmartlightAlarmActionOutcomeUnknown(RuntimeError):
    pass


class SmartlightCentralAdapter:
    def __init__(self, *, base_url: str, allow_insecure_http: bool = False) -> None:
        parsed = urlparse(str(base_url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Smartlight base URL must use http(s)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Smartlight base URL is invalid")
        if parsed.path.rstrip("/") != "/smartlight":
            raise ValueError("Smartlight base URL must end with /smartlight")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ValueError(
                "Smartlight plain HTTP requires explicit allow_insecure_http opt-in"
            )
        self.origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        self.base_url = f"{self.origin}/smartlight/"
        self.allowed_origins = {self.origin}

    def authentication_contract(self) -> dict:
        entry_url = f"{self.origin}/smartlight/"
        return {
            "system_id": SMARTLIGHT_SYSTEM_ID,
            "system_name": SMARTLIGHT_SYSTEM_NAME,
            "origin": self.origin,
            "page_fingerprint": "smartlight-cas-captcha-v1",
            "fields": [
                {
                    "name": "username",
                    "label": "系统账号",
                    "input_type": "text",
                    "autocomplete": "username",
                    "required": True,
                },
                {
                    "name": "password",
                    "label": "密码",
                    "input_type": "password",
                    "autocomplete": "current-password",
                    "required": True,
                },
                {
                    "name": "authcode",
                    "label": "验证码",
                    "input_type": "text",
                    "autocomplete": "off",
                    "required": True,
                },
            ],
            "prepared_authentication": {
                "kind": "image_captcha",
                "entry_url": entry_url,
            },
        }

    def prepare_authentication(self, worker, *, timeout_seconds: float) -> dict:
        response = worker.request_bytes(
            "GET",
            self.base_url,
            headers=_CAS_HTML_HEADERS,
            timeout_seconds=timeout_seconds,
        )
        response = self._follow_login_redirects(
            worker,
            response,
            timeout_seconds=timeout_seconds,
        )
        if response["status"] != 200:
            raise SmartlightSessionCheckUnavailable(
                "Smartlight CAS login page is temporarily unavailable."
            )
        parser = _CasLoginParser()
        parser.feed(response["body"].decode("utf-8", errors="replace"))
        parser.close()
        if not parser.form_action or not parser.captcha_src:
            raise SmartlightLoginContractMismatch(
                "CAS login page no longer exposes the registered captcha form."
            )
        required_hidden = {"lt", "execution", "_eventId"}
        if not required_hidden.issubset(parser.hidden_fields):
            raise SmartlightLoginContractMismatch(
                "CAS login page is missing required hidden fields."
            )
        page_url = str(response.get("url") or self.base_url)
        form_url = _same_origin_url(
            self.origin,
            urljoin(page_url, parser.form_action),
        )
        captcha_url = _same_origin_url(
            self.origin,
            urljoin(page_url, parser.captcha_src),
        )
        separator = "&" if "?" in captcha_url else "?"
        captcha = worker.request_bytes(
            "GET",
            f"{captcha_url}{separator}agentbridge={quote(str(datetime.now(timezone.utc).timestamp()))}",
            headers={
                **_CAS_HTML_HEADERS,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
                "Referer": page_url,
            },
            timeout_seconds=timeout_seconds,
        )
        content_type = str(captcha.get("content_type") or "").split(";", 1)[0].lower()
        body = captcha.get("body")
        if captcha.get("status") != 200 or content_type not in {
            "image/jpeg",
            "image/png",
            "image/gif",
        } or not isinstance(body, bytes) or not body:
            raise SmartlightSessionCheckUnavailable(
                "Smartlight CAPTCHA image is temporarily unavailable."
            )
        worker.set_http_state(
            {
                "smartlight_prepared_login": {
                    "form_url": form_url,
                    "login_page_url": page_url,
                    "hidden_fields": {
                        name: parser.hidden_fields[name]
                        for name in sorted(required_hidden)
                    },
                    "captcha_content_type": content_type,
                    "captcha_body_base64": base64.b64encode(body).decode("ascii"),
                }
            }
        )
        return {
            "captcha": {"content_type": content_type, "body": body},
        }

    def recover_prepared_authentication(self, worker) -> dict | None:
        state = worker.get_http_state()
        prepared = state.get("smartlight_prepared_login")
        if not isinstance(prepared, dict):
            return None
        content_type = str(prepared.get("captcha_content_type") or "").lower()
        encoded = str(prepared.get("captcha_body_base64") or "")
        if content_type not in {"image/jpeg", "image/png", "image/gif"} or not encoded:
            return None
        try:
            body = base64.b64decode(encoded, validate=True)
        except ValueError:
            return None
        if not body or len(body) > 256 * 1024:
            return None
        return {"captcha": {"content_type": content_type, "body": body}}

    def authenticate(
        self,
        worker,
        credentials: dict,
        *,
        timeout_seconds: float,
    ) -> dict:
        state = worker.get_http_state()
        prepared = state.get("smartlight_prepared_login")
        if not isinstance(prepared, dict):
            raise SmartlightLoginContractMismatch(
                "Prepared CAS login state is unavailable."
            )
        form_url = _same_origin_url(self.origin, prepared.get("form_url"))
        login_page_url = _same_origin_url(
            self.origin,
            prepared.get("login_page_url"),
        )
        hidden = prepared.get("hidden_fields")
        if not isinstance(hidden, dict) or set(hidden) != {"lt", "execution", "_eventId"}:
            raise SmartlightLoginContractMismatch(
                "Prepared CAS login fields are invalid."
            )
        username = str(credentials.get("username") or "").strip()
        password = str(credentials.get("password") or "")
        authcode = str(credentials.get("authcode") or "").strip()
        if not username or not password or not authcode:
            raise ValueError("username, password, and authcode are required")

        # The legacy CAS page applies these transformations in JavaScript.
        payload = {
            **hidden,
            "username": base64.b64encode(username.encode("utf-8")).decode("ascii"),
            "password": hashlib.md5(password.encode("utf-8")).hexdigest().upper(),  # noqa: S324
            "authcode": authcode,
            "submit": "登录",
        }
        response = worker.request_bytes(
            "POST",
            form_url,
            headers={
                **_CAS_HTML_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.origin,
                "Referer": login_page_url,
            },
            body=urlencode(payload),
            timeout_seconds=timeout_seconds,
        )
        response = self._follow_login_redirects(
            worker,
            response,
            timeout_seconds=timeout_seconds,
        )
        if response["status"] != 200:
            raise SmartlightAuthenticationRejected(
                "The system rejected the authentication request."
            )
        if "/cas/login" in response["url"]:
            parser = _CasLoginParser()
            parser.feed(response["body"].decode("utf-8", errors="replace"))
            parser.close()
            error_code = _login_rejection_code(parser.error_message)
            raise SmartlightAuthenticationRejected(
                "The system rejected the account, password, or verification code.",
                error_code=error_code,
            )
        probe = self.probe_session(worker)
        return {
            "observed_principal_ref": probe["observed_principal_ref"],
            "principal": probe["principal"],
            "transport": "central_cas_cookie_jwt",
        }

    def probe_session(self, worker) -> dict:
        refresh_error: Exception | None = None
        try:
            token_state = self._refresh_token(worker)
        except (
            SmartlightLoginRequired,
            SmartlightSessionCheckUnavailable,
            SmartlightLoginContractMismatch,
        ) as exc:
            refresh_error = exc
            token_state = None
        if token_state is None:
            try:
                principal, application_session_renewed = (
                    self._cas_principal_with_renewal(worker)
                )
                token_state = self._exchange_token(worker, principal)
                token_state["session_recovery"] = (
                    "cas_sso" if application_session_renewed else None
                )
            except SmartlightLoginRequired as exc:
                if refresh_error is None:
                    raise
                if isinstance(refresh_error, SmartlightLoginRequired):
                    raise SmartlightLoginRequired(
                        "Smartlight refresh token and CAS session are no longer "
                        f"authenticated ({_session_error_summary(refresh_error)}; "
                        f"{_session_error_summary(exc)})."
                    ) from exc
                raise SmartlightSessionCheckUnavailable(
                    "Smartlight token refresh could not be confirmed and the CAS "
                    "fallback is not authenticated; the encrypted session was "
                    f"preserved for retry ({_session_error_summary(refresh_error)}; "
                    f"{_session_error_summary(exc)})."
                ) from exc
            except (
                SmartlightSessionCheckUnavailable,
                SmartlightLoginContractMismatch,
            ) as exc:
                if refresh_error is not None:
                    raise SmartlightSessionCheckUnavailable(
                        "Smartlight token refresh and CAS fallback are temporarily "
                        f"unavailable ({_session_error_summary(refresh_error)}; "
                        f"{_session_error_summary(exc)})."
                    ) from exc
                raise
        worker.set_http_state(token_state)
        return self._probe_result(token_state)

    def keepalive_session(self, worker) -> dict:
        """Keep the parent CAS session alive and renew the complete JWT pair."""
        try:
            principal, application_session_renewed = (
                self._cas_principal_with_renewal(worker)
            )
            token_state = self._exchange_token(worker, principal)
            token_state["session_recovery"] = (
                "cas_sso" if application_session_renewed else None
            )
        except (
            SmartlightLoginRequired,
            SmartlightSessionCheckUnavailable,
            SmartlightLoginContractMismatch,
        ):
            return self.probe_session(worker)
        worker.set_http_state(token_state)
        return self._probe_result(token_state)

    @staticmethod
    def _probe_result(token_state: dict) -> dict:
        return {
            "authenticated": True,
            "observed_principal_ref": token_state["principal"]["name"],
            "principal": token_state["principal"],
            "template_count": None,
            "transport": "central_cas_cookie_jwt",
            "session_recovery": token_state.get("session_recovery"),
        }

    def _refresh_token(self, worker) -> dict | None:
        state = worker.get_http_state()
        refresh_token = str(state.get("refresh_token") or "").strip()
        principal = state.get("principal")
        if not refresh_token or not isinstance(principal, dict):
            return None
        if _jwt_is_expired(refresh_token):
            raise SmartlightLoginRequired(
                "Smartlight refresh token has expired."
            )
        response = worker.request(
            "POST",
            f"{self.origin}/jwtcenter//JWTInfoController/refreshToken",
            headers=_FORM_HEADERS,
            body=urlencode({"refresh_token": refresh_token}),
            timeout_seconds=20,
        )
        if response["status"] in {301, 302, 303, 307, 308, 401, 403}:
            raise SmartlightLoginRequired(
                "Smartlight refresh token is no longer accepted."
            )
        payload = response.get("json")
        if _refresh_response_requires_login(payload):
            raise SmartlightLoginRequired(
                "Smartlight refresh token has expired or is no longer accepted."
            )
        data = payload.get("resp_data") if isinstance(payload, dict) else None
        if response["status"] != 200 or not isinstance(data, dict):
            response_code = (
                str(payload.get("resp_code"))
                if isinstance(payload, dict) and payload.get("resp_code") is not None
                else "unknown"
            )
            raise SmartlightSessionCheckUnavailable(
                "Smartlight token refresh service is temporarily unavailable "
                f"(HTTP {response['status']}, code={response_code})."
            )
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise SmartlightLoginContractMismatch(
                "Smartlight token refresh response has no access token."
            )
        rotated_refresh_token = str(data.get("refresh_token") or "").strip()
        return {
            "access_token": access_token,
            "refresh_token": rotated_refresh_token or refresh_token,
            "access_token_duration": (
                data.get("access_token_duration")
                if data.get("access_token_duration") is not None
                else state.get("access_token_duration")
            ),
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "principal": deepcopy(principal),
        }

    def invoke_capability(self, capability_name: str, worker, arguments: dict) -> dict:
        if capability_name == SMARTLIGHT_OVERVIEW_CAPABILITY:
            return self.system_overview(worker)
        if capability_name == SMARTLIGHT_RUNTIME_OVERVIEW_CAPABILITY:
            return self.runtime_overview(worker)
        if capability_name == SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY:
            return self.list_rtu_status(worker, arguments)
        if capability_name == SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY:
            return self.list_lamp_status(worker, arguments)
        if capability_name == SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY:
            return self.list_lamp_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_LAMP_ALARM_ANALYSIS_CAPABILITY:
            return self.analyze_lamp_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_RTU_SURVEY_RECORDS_CAPABILITY:
            return self.list_rtu_survey_records(worker, arguments)
        if capability_name == SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY:
            return self.list_energy_records(worker, arguments)
        if capability_name == SMARTLIGHT_ENERGY_ANALYSIS_CAPABILITY:
            return self.analyze_energy(worker, arguments)
        if capability_name == SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY:
            return self.list_lamp_survey_records(worker, arguments)
        if capability_name == SMARTLIGHT_RTU_LEAKAGE_ALARM_LIST_CAPABILITY:
            return self.list_rtu_leakage_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_RTU_LEAKAGE_ANALYSIS_CAPABILITY:
            return self.analyze_rtu_leakage_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_OFF_HOURS_CURRENT_LIST_CAPABILITY:
            return self.list_off_hours_currents(worker, arguments)
        if capability_name == SMARTLIGHT_INSPECTION_LOG_LIST_CAPABILITY:
            return self.list_inspection_logs(worker, arguments)
        if capability_name == SMARTLIGHT_MAINTENANCE_RECORD_LIST_CAPABILITY:
            return self.list_maintenance_records(worker, arguments)
        if capability_name == SMARTLIGHT_LAMPPOST_LIST_CAPABILITY:
            return self.list_lampposts(worker, arguments)
        if capability_name == SMARTLIGHT_ALARM_LIST_CAPABILITY:
            return self.list_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_ALARM_REMARK_GET_CAPABILITY:
            return self.read_alarm_remark(worker, arguments)
        if capability_name == SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY:
            return self.list_inspection_tasks(worker, arguments)
        if capability_name == SMARTLIGHT_LEAKAGE_SUMMARY_CAPABILITY:
            return self.leakage_summary(worker, arguments)
        if capability_name == SMARTLIGHT_ASSET_SEARCH_CAPABILITY:
            return self.search_assets(worker, arguments)
        if capability_name == SMARTLIGHT_ASSET_DETAIL_CAPABILITY:
            return self.asset_detail(worker, arguments)
        if capability_name == SMARTLIGHT_ALARM_ANALYSIS_CAPABILITY:
            return self.analyze_alarms(worker, arguments)
        if capability_name == SMARTLIGHT_INSPECTION_TASK_DETAIL_CAPABILITY:
            return self.inspection_task_detail(worker, arguments)
        if capability_name == SMARTLIGHT_LEAKAGE_ANALYSIS_CAPABILITY:
            return self.analyze_leakage(worker, arguments)
        if capability_name == SMARTLIGHT_REPORT_EXPORT_CAPABILITY:
            return self.export_report(worker, arguments)
        raise KeyError(f"unsupported Smartlight capability: {capability_name}")

    def system_overview(self, worker) -> dict:
        context = self._principal_context(worker)
        cabinets = self._authorized_post_json(
            worker,
            "/map/getIntegratedRControlCabinetDataByCondition",
            {
                "findFlag": "true",
                "json": _json_text(
                    {
                        "_like_controlCabinetName": "",
                        "organroleId": context["organroleId"],
                    }
                ),
            },
        )
        lampposts = self._authorized_post_json(
            worker,
            "/lLamppost/getLampDetialList",
            {
                "json": _json_text({"lampTypeIds": [], "streetIds": []}),
                "pageNum": 1,
                "pageSize": 1,
                "organroleId": context["organroleId"],
            },
        )
        searchable_lampposts = self._authorized_post_json(
            worker,
            "/lLamppost/getDataByConditionForFacilityEx",
            {
                "json": _json_text(_lamppost_filters(context, "")),
                "orderBy": "lamp_post_code",
                "pageNum": 1,
                "pageSize": 1,
                "organroleId": context["organroleId"],
            },
        )
        cabinet_items = cabinets if isinstance(cabinets, list) else []
        map_lamp_total = _page_total(lampposts)
        searchable_lamp_total = _page_total(searchable_lampposts)
        return {
            "principal": context,
            "cabinetTotal": len(cabinet_items),
            "lampPostTotal": searchable_lamp_total,
            "lampPostCounts": {
                "searchable": searchable_lamp_total,
                "mapDetail": map_lamp_total,
            },
            "countSemantics": {
                "lampPostTotal": "searchable_lamppost_list",
                "lampPostCounts.mapDetail": "map_detail_inventory",
            },
            "cabinetStates": _count_values(
                cabinet_items,
                ("onlineState", "state", "rtuState", "alarmState"),
            ),
            "sampleCabinets": [
                _normalize_cabinet(item)
                for item in cabinet_items[:10]
                if isinstance(item, dict)
            ],
        }

    def runtime_overview(self, worker) -> dict:
        context = self._principal_context(worker)
        rtu_query = _rtu_status_query()
        rtu_counts = self._authorized_post_json(
            worker,
            "/rRtu/getRtuStateCountDataByConditionNew",
            {
                "json": _json_text(rtu_query),
                "organroleId": context["organroleId"],
            },
        )
        lamp_counts = self._authorized_post_json(
            worker,
            "/lLamppost/newGetTotalStatus",
            {
                "json": _json_text({"lampTypeIds": [], "streetIds": []}),
                "organroleId": context["organroleId"],
            },
        )
        observed_at = datetime.now(timezone.utc).isoformat()
        return {
            "scope": "authenticated_user_runtime_pages",
            "observedAt": observed_at,
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "rtu": {
                "total": _smartlight_int(_first(rtu_counts, "rAllCount")),
                "online": _smartlight_int(_first(rtu_counts, "rOnlineCount")),
                "offline": _smartlight_int(
                    _first(rtu_counts, "rNoOnlineWithNoHandleCount")
                ),
                "powerOff": _smartlight_int(
                    _first(rtu_counts, "rPowerOutageCount")
                ),
                "disabled": _smartlight_int(_first(rtu_counts, "rDisableCount")),
                "onlineWithElectricity": _smartlight_int(
                    _first(rtu_counts, "rOnlineWithEleCount")
                ),
                "onlineWithoutElectricity": _smartlight_int(
                    _first(rtu_counts, "rOnlineWithOutEleCount")
                ),
                "notCalculated": _smartlight_int(
                    _first(rtu_counts, "rOnlineNoCalCount")
                ),
                "source": "/rRtu/getRtuStateCountDataByConditionNew",
                "scope": "lighting_runtime_rtu",
            },
            "singleLamp": {
                "controllerTotal": _smartlight_int(
                    _first(lamp_counts, "AllControlCount")
                ),
                "controllerOnline": _smartlight_int(
                    _first(lamp_counts, "OnlineCount")
                ),
                "controllerOffline": _smartlight_int(
                    _first(lamp_counts, "OfflineCount")
                ),
                "lampTotal": _smartlight_int(_first(lamp_counts, "AlonelampCount")),
                "lampOn": _smartlight_int(_first(lamp_counts, "OpenLampCount")),
                "lampOff": _smartlight_int(_first(lamp_counts, "CloseLampCount")),
                "lampPostTotal": _smartlight_int(
                    _first(lamp_counts, "LampPostCount")
                ),
                "singleLampPosts": _smartlight_int(
                    _first(lamp_counts, "SingleLampPostCount")
                ),
                "doubleLampPosts": _smartlight_int(
                    _first(lamp_counts, "DoubleLampPostCount")
                ),
                "tripleLampPosts": _smartlight_int(
                    _first(lamp_counts, "TribleLampPostCount")
                ),
                "otherLampPosts": _smartlight_int(
                    _first(lamp_counts, "OtherLampPostCount")
                ),
                "alarmLampPosts": _smartlight_int(
                    _first(lamp_counts, "alarmLampPostCount")
                ),
                "source": "/lLamppost/newGetTotalStatus",
                "scope": "single_lamp_runtime",
            },
            "semantics": {
                "countsAreDownstreamSnapshots": True,
                "derivedCountsAdded": False,
                "assetInventoryComparable": False,
            },
        }

    def list_rtu_status(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        keyword = str(arguments.get("keyword") or "").strip()
        state = _normalize_choice(
            arguments.get("state"),
            default="all",
            allowed={"all", "online", "offline", "power_off", "disabled"},
            field_name="state",
        )
        alarm_only = bool(arguments.get("alarm_only"))
        work_area = str(arguments.get("work_area") or "").strip()
        group = str(arguments.get("group") or "").strip()
        model = str(arguments.get("model") or "").strip()
        query = _rtu_status_query(keyword=keyword)
        local_filter = alarm_only or state == "online" or any(
            (work_area, group, model)
        )
        if local_filter:
            downstream_filters = {
                "online": ["OnlineWithElc", "OffLamppost"],
                "offline": ["NoOnlineWithNoHandle"],
                "power_off": ["PowerOutage"],
                "disabled": ["Disable"],
            }.get(state, [None])
            records: list[dict] = []
            downstream_total = 0
            truncated = False
            seen: set[str] = set()
            for downstream_filter in downstream_filters:
                payload = self._rtu_status_page(
                    worker,
                    context,
                    query=query,
                    downstream_filter=downstream_filter,
                    page=1,
                    size=_SMARTLIGHT_RUNTIME_SCAN_LIMIT,
                )
                downstream_total += _page_total(payload)
                truncated = truncated or _page_total(payload) > len(_page_items(payload))
                for item in _page_items(payload):
                    key = str(_first(item, "rtuId", "id") or id(item))
                    if key not in seen:
                        records.append(item)
                        seen.add(key)
            normalized = [_normalize_rtu_status(item, requested_state=state) for item in records]
            if alarm_only:
                normalized = [item for item in normalized if item["hasAlarm"] is True]
            normalized = [
                item
                for item in normalized
                if _contains_text(item.get("workArea"), work_area)
                and _contains_text(item.get("group"), group)
                and _contains_text(item.get("model"), model)
            ]
            start = (page - 1) * size
            selected = normalized[start : start + size]
            total = len(normalized)
        else:
            downstream_filter = _rtu_state_filter(state)
            payload = self._rtu_status_page(
                worker,
                context,
                query=query,
                downstream_filter=downstream_filter,
                page=page,
                size=size,
            )
            selected = [
                _normalize_rtu_status(item, requested_state=state)
                for item in _page_items(payload)
            ]
            downstream_total = _page_total(payload)
            total = downstream_total
            truncated = False
        return {
            "scope": "lighting_runtime_rtu",
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "filters": {
                "keyword": keyword or None,
                "state": state,
                "alarmOnly": alarm_only,
                "workArea": work_area or None,
                "group": group or None,
                "model": model or None,
            },
            "page": page,
            "size": size,
            "total": total,
            "downstreamTotal": downstream_total,
            "count": len(selected),
            "truncated": truncated,
            "source": "/rRtu/getRtusByConditionNew",
            "items": selected,
        }

    def _rtu_status_page(
        self,
        worker,
        context: dict,
        *,
        query: dict,
        downstream_filter: str | None,
        page: int,
        size: int,
    ) -> Any:
        request_query = dict(query)
        request_query["filterParam"] = downstream_filter
        return self._authorized_post_json(
            worker,
            "/rRtu/getRtusByConditionNew",
            {
                "json": _json_text(request_query),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )

    def list_lamp_status(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        keyword = str(arguments.get("keyword") or "").strip()
        controller_state = _normalize_choice(
            arguments.get("controller_state"),
            default="all",
            allowed={"all", "online", "offline"},
            field_name="controller_state",
        )
        lamp_state = _normalize_choice(
            arguments.get("lamp_state"),
            default="all",
            allowed={"all", "on", "off", "abnormal"},
            field_name="lamp_state",
        )
        alarm_only = bool(arguments.get("alarm_only"))
        street = str(arguments.get("street") or "").strip()
        cabinet = str(arguments.get("cabinet") or "").strip()
        work_area = str(arguments.get("work_area") or "").strip()
        query = {
            "_like_params": keyword,
            "_include_leffecteId": [],
            "_include_streetId": [],
            "_include_controlCabinetId": [],
            "_include_workAreaId": [],
            "lampTypeIds": [],
            "streetIds": [],
        }
        status = _lamp_status_filter(controller_state, lamp_state)
        local_filter = (
            alarm_only
            or lamp_state == "abnormal"
            or (controller_state != "all" and lamp_state != "all")
            or any((street, cabinet, work_area))
        )
        if local_filter:
            payload = self._lamp_status_page(
                worker,
                context,
                query=query,
                status=status,
                page=1,
                size=_SMARTLIGHT_RUNTIME_SCAN_LIMIT,
            )
            normalized = [_normalize_lamp_status(item) for item in _page_items(payload)]
            normalized = [
                item
                for item in normalized
                if _lamp_status_matches(
                    item,
                    controller_state=controller_state,
                    lamp_state=lamp_state,
                    alarm_only=alarm_only,
                )
                and _contains_text(item.get("road"), street)
                and _contains_text(item.get("cabinet"), cabinet)
                and _contains_text(item.get("workArea"), work_area)
            ]
            start = (page - 1) * size
            selected = normalized[start : start + size]
            downstream_total = _page_total(payload)
            total = len(normalized)
            truncated = downstream_total > len(_page_items(payload))
        else:
            payload = self._lamp_status_page(
                worker,
                context,
                query=query,
                status=status,
                page=page,
                size=size,
            )
            selected = [
                _normalize_lamp_status(item) for item in _page_items(payload)[:size]
            ]
            downstream_total = _page_total(payload)
            total = downstream_total
            truncated = False
        return {
            "scope": "single_lamp_runtime",
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "filters": {
                "keyword": keyword or None,
                "controllerState": controller_state,
                "lampState": lamp_state,
                "alarmOnly": alarm_only,
                "street": street or None,
                "cabinet": cabinet or None,
                "workArea": work_area or None,
            },
            "page": page,
            "size": size,
            "total": total,
            "downstreamTotal": downstream_total,
            "count": len(selected),
            "truncated": truncated,
            "source": "/lLamppost/getLampDetialList",
            "items": selected,
        }

    def _lamp_status_page(
        self,
        worker,
        context: dict,
        *,
        query: dict,
        status: str | None,
        page: int,
        size: int,
    ) -> Any:
        fields: dict[str, Any] = {
            "json": _json_text(query),
            "pageNum": page,
            "pageSize": size,
            "organroleId": context["organroleId"],
        }
        if status:
            fields["status"] = status
        return self._authorized_post_json(
            worker,
            "/lLamppost/getLampDetialList",
            fields,
        )

    def list_lamp_alarms(self, worker, arguments: dict) -> dict:
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        normalized, metadata = self._lamp_alarm_records(worker, arguments)
        start = (page - 1) * size
        selected = normalized[start : start + size]
        return {
            **metadata,
            "page": page,
            "size": size,
            "total": len(normalized),
            "count": len(selected),
            "items": selected,
        }

    def analyze_lamp_alarms(self, worker, arguments: dict) -> dict:
        top_n = _bounded_int(arguments.get("top_n"), default=10, minimum=1, maximum=20)
        normalized, metadata = self._lamp_alarm_records(worker, arguments)
        result = {
            **metadata,
            "analyzedCount": len(normalized),
            "dailyTrend": _daily_counts(normalized, "lastActivityAt"),
            "topAlarmTypes": _top_counts(normalized, "alarmType", top_n),
            "topLampPosts": _top_counts(normalized, "lampPost", top_n),
            "topRoads": _top_counts(normalized, "road", top_n),
            "stateCounts": _top_counts(normalized, "stateLabel", top_n),
            "recentAlarms": normalized[:20],
        }
        if arguments.get("_include_report_rows") is True:
            result["_reportRows"] = normalized
        return result

    def _lamp_alarm_records(self, worker, arguments: dict) -> tuple[list[dict], dict]:
        context = self._principal_context(worker)
        start_date, end_date, range_source, last_days = _resolve_analysis_date_range(
            arguments
        )
        alarm_state = _normalize_choice(
            arguments.get("alarm_state"),
            default="all",
            allowed={"all", "current", "non_current"},
            field_name="alarm_state",
        )
        query = {
            "_include_controlCabinetId": [],
            "_like_lampPostCode": str(arguments.get("keyword") or "").strip(),
            "_timebegin_alarmAddDate": "",
            "_timeend_alarmAddDate": "",
            "_include_alarmState": (
                [0] if alarm_state == "current" else [1] if alarm_state == "non_current" else [0, 1]
            ),
            "_include_duration": [0],
            "_include_hitchDicId": [],
            "_include_streetId": [],
            "_include_workId": [],
            "_timebegin_lastDate": f"{start_date} 00:00:00",
            "_timeend_lastDate": f"{end_date} 23:59:59",
            "_show_newData": True,
            "_leakage_threshold": 0,
            "_leakage_current": 0,
            "_duration": 0,
            "userId": context["userId"],
        }
        raw_items: list[dict] = []
        downstream_total = 0
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // _SMARTLIGHT_ANALYSIS_PAGE_SIZE) + 1):
            payload = self._authorized_post_json(
                worker,
                "/lHisHitchAlarm/getDataByCondition",
                {
                    "json": _json_text(query),
                    "orderBy": "l_his_coplog.cop_date",
                    "pageNum": page,
                    "pageSize": _SMARTLIGHT_ANALYSIS_PAGE_SIZE,
                    "organroleId": context["organroleId"],
                },
            )
            page_items = _page_items(payload)
            if page == 1:
                downstream_total = _page_total(payload)
            raw_items.extend(page_items)
            if (
                not page_items
                or len(raw_items) >= downstream_total
                or len(raw_items) >= _SMARTLIGHT_ANALYSIS_LIMIT
            ):
                break
        normalized = [_normalize_lamp_alarm(item) for item in raw_items]
        for argument_name, item_field in (
            ("alarm_type", "alarmType"),
            ("road", "road"),
            ("work_area", "workArea"),
            ("cabinet", "cabinet"),
        ):
            needle = str(arguments.get(argument_name) or "").strip().casefold()
            if needle:
                normalized = [
                    item
                    for item in normalized
                    if needle in str(item.get(item_field) or "").casefold()
                ]
        normalized.sort(
            key=lambda item: (
                str(item.get("lastActivityAt") or ""),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        return normalized, {
            "alarmSource": "single_lamp",
            "dateRange": {
                "source": range_source,
                "lastDays": last_days,
                "startDate": start_date,
                "endDate": end_date,
                "inclusive": True,
                "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            },
            "filters": {
                "keyword": arguments.get("keyword") or None,
                "alarmType": arguments.get("alarm_type") or None,
                "alarmState": alarm_state,
                "road": arguments.get("road") or None,
                "workArea": arguments.get("work_area") or None,
                "cabinet": arguments.get("cabinet") or None,
            },
            "downstreamTotal": downstream_total,
            "retrievedCount": len(raw_items),
            "truncated": downstream_total > len(raw_items),
            "analysisLimit": _SMARTLIGHT_ANALYSIS_LIMIT,
            "source": "/lHisHitchAlarm/getDataByCondition",
        }

    def list_rtu_survey_records(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_time, end_time, range_source = _resolve_survey_time_range(arguments)
        rtu_id = str(arguments.get("rtu_id") or "").strip()
        rtu_keyword = str(arguments.get("rtu_keyword") or "").strip()
        candidates: list[dict] = []
        if not rtu_id:
            if not rtu_keyword:
                raise ValueError("rtu_id or rtu_keyword is required")
            candidate_payload = self._rtu_status_page(
                worker,
                context,
                query=_rtu_status_query(keyword=rtu_keyword),
                downstream_filter=None,
                page=1,
                size=100,
            )
            candidates = [
                _normalize_rtu_status(item, requested_state="all")
                for item in _page_items(candidate_payload)
            ]
            if len(candidates) != 1:
                return {
                    "resolved": False,
                    "resolution": "not_found" if not candidates else "ambiguous",
                    "rtuKeyword": rtu_keyword,
                    "candidateTotal": _page_total(candidate_payload),
                    "candidates": candidates[:20],
                    "message": (
                        "未找到匹配 RTU。" if not candidates else "匹配到多个 RTU，请使用精确 rtu_id。"
                    ),
                }
            rtu_id = str(candidates[0].get("id") or "")
        start_date, start_clock = start_time.split(" ", 1)
        end_date, end_clock = end_time.split(" ", 1)
        query = {
            "isOnlyAbnormal": "1" if arguments.get("abnormal_only") else "0",
            "isConbineTime": "1",
            "isRatingData": "1",
            "isRtuRelayI": "0",
            "isRtuRoadI": "1",
            "_like_params": rtu_keyword,
            "_timebegin_addDate": start_date,
            "_timebegin_addTime": start_clock,
            "_timeend_addDate": end_date,
            "_timeend_addTime": end_clock,
            "_dataArray": [],
            "dataArray": [],
            "selectDataArray": ["2"],
            "selectStreetArray": [],
            "_timebegin_addDateTime": start_time,
            "_timeend_addDateTime": end_time,
            "rtuId": rtu_id,
        }
        payload = self._authorized_post_json(
            worker,
            "/rHisCoplogPhase/getDataByCondition",
            {
                "json": _json_text(query),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        items = [_normalize_rtu_survey(item) for item in _page_items(payload)]
        return {
            "resolved": True,
            "scope": "rtu_survey_history",
            "rtuId": rtu_id,
            "rtuKeyword": rtu_keyword or None,
            "resolvedRtu": candidates[0] if candidates else None,
            "dateRange": {
                "source": range_source,
                "startTime": start_time,
                "endTime": end_time,
                "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
                "maximumDays": _SMARTLIGHT_SURVEY_MAX_DAYS,
            },
            "page": page,
            "size": size,
            "total": _page_total(payload),
            "count": len(items),
            "source": "/rHisCoplogPhase/getDataByCondition",
            "items": items,
        }

    def list_energy_records(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_date, end_date, range_source, last_days = _resolve_bounded_date_range(
            arguments,
            default_days=7,
            maximum_days=_SMARTLIGHT_ENERGY_RECORD_MAX_DAYS,
            range_name="energy record",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        device_id = str(arguments.get("device_id") or "").strip()
        payload = self._energy_record_page(
            worker,
            context,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            device_id=device_id,
            page=page,
            size=size,
        )
        items = [_normalize_energy_record(item) for item in _page_items(payload)]
        return {
            "scope": "rtu_energy_records",
            "filters": {
                "keyword": keyword or None,
                "deviceId": device_id or None,
                "startDate": start_date,
                "endDate": end_date,
                "rangeSource": range_source,
                "lastDays": last_days,
                "granularity": "day",
            },
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "page": page,
            "size": size,
            "downstreamTotal": _page_total(payload),
            "returnedCount": len(items),
            "count": len(items),
            "truncated": False,
            "sourcePage": "智能监控管理/查询统计/用电量记录",
            "sourceKind": "downstream_daily_energy_matrix",
            "warnings": _energy_warnings(items),
            "items": items,
        }

    def analyze_energy(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        start_date, end_date, range_source, last_days = _resolve_bounded_date_range(
            arguments,
            default_days=30,
            maximum_days=_SMARTLIGHT_ENERGY_ANALYSIS_MAX_DAYS,
            range_name="energy analysis",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        device_id = str(arguments.get("device_id") or "").strip()
        top_n = _bounded_int(arguments.get("top_n"), default=10, minimum=1, maximum=20)
        raw_items, downstream_total = self._collect_energy_records(
            worker,
            context,
            start_date=start_date,
            end_date=end_date,
            keyword=keyword,
            device_id=device_id,
        )
        items = [_normalize_energy_record(item) for item in raw_items]
        trend_totals: dict[str, float] = {}
        trend_counts: Counter[str] = Counter()
        device_totals: list[dict] = []
        units = {
            str(item.get("unit"))
            for item in items
            if item.get("unit") not in (None, "")
        }
        for item in items:
            numeric_total = 0.0
            numeric_count = 0
            for point in item.get("periodValues") or []:
                numeric = _smartlight_number(point.get("value"))
                if numeric is None:
                    continue
                date = str(point.get("date") or "")
                trend_totals[date] = trend_totals.get(date, 0.0) + numeric
                trend_counts[date] += 1
                numeric_total += numeric
                numeric_count += 1
            if numeric_count:
                device_totals.append(
                    {
                        "deviceId": item.get("deviceId"),
                        "deviceName": item.get("deviceName"),
                        "deviceCode": item.get("deviceCode"),
                        "value": _rounded_number(numeric_total),
                        "dataPointCount": numeric_count,
                        "unit": item.get("unit"),
                    }
                )
        device_totals.sort(
            key=lambda item: (
                float(item.get("value") or 0),
                str(item.get("deviceName") or ""),
            ),
            reverse=True,
        )
        trend = [
            {
                "date": date,
                "value": _rounded_number(trend_totals[date]),
                "dataPointCount": trend_counts[date],
                "unit": next(iter(units)) if len(units) == 1 else None,
            }
            for date in sorted(trend_totals)
        ]
        warnings = _energy_warnings(items)
        if len(units) > 1:
            warnings.append("下游返回了不一致的能耗单位，汇总值未标记统一单位。")
        return {
            "scope": "rtu_energy_analysis",
            "filters": {
                "keyword": keyword or None,
                "deviceId": device_id or None,
                "startDate": start_date,
                "endDate": end_date,
                "rangeSource": range_source,
                "lastDays": last_days,
                "granularity": "day",
            },
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "downstreamTotal": downstream_total,
            "retrievedCount": len(raw_items),
            "analyzedCount": len(items),
            "truncated": downstream_total > len(raw_items),
            "analysisLimit": _SMARTLIGHT_ANALYSIS_LIMIT,
            "sourcePage": "智能监控管理/查询统计/用电量记录",
            "sourceKind": "bounded_downstream_daily_energy_analysis",
            "valueSemantics": "页面按日直接返回的用电显示值，未由累计读数差分推算。",
            "unit": next(iter(units)) if len(units) == 1 else None,
            "totalValue": (
                _rounded_number(sum(trend_totals.values())) if trend_totals else None
            ),
            "dailyTrend": trend,
            "topDevices": device_totals[:top_n],
            "warnings": warnings,
        }

    def _collect_energy_records(
        self,
        worker,
        context: dict,
        *,
        start_date: str,
        end_date: str,
        keyword: str,
        device_id: str,
    ) -> tuple[list[dict], int]:
        records: list[dict] = []
        downstream_total = 0
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // 100) + 1):
            payload = self._energy_record_page(
                worker,
                context,
                start_date=start_date,
                end_date=end_date,
                keyword=keyword,
                device_id=device_id,
                page=page,
                size=100,
            )
            page_items = _page_items(payload)
            if page == 1:
                downstream_total = _page_total(payload)
            records.extend(page_items)
            if (
                not page_items
                or len(records) >= downstream_total
                or len(records) >= _SMARTLIGHT_ANALYSIS_LIMIT
            ):
                break
        return records[:_SMARTLIGHT_ANALYSIS_LIMIT], downstream_total

    def _energy_record_page(
        self,
        worker,
        context: dict,
        *,
        start_date: str,
        end_date: str,
        keyword: str,
        device_id: str,
        page: int,
        size: int,
    ) -> Any:
        query = {
            "_like_params": keyword,
            "startReportDate": start_date,
            "endReportDate": end_date,
            "reportTimeType": "D",
            "organroleId": context["organroleId"],
            "isTodayEnergy": "0",
            "rtuIds": [device_id] if device_id else [],
        }
        return self._authorized_post_json(
            worker,
            "/rEnergyReport/getDateByEnergy",
            {
                "json": _json_text(query),
                "organroleId": context["organroleId"],
                "_like_params": keyword,
                "pageNum": page,
                "pageSize": size,
            },
        )

    def list_lamp_survey_records(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_time, end_time, range_source = _resolve_bounded_time_range(
            arguments,
            default_hours=24,
            maximum_days=_SMARTLIGHT_SURVEY_MAX_DAYS,
            range_name="single-lamp survey",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        lamp_post_id = str(arguments.get("lamp_post_id") or "").strip()
        payload = self._authorized_post_json(
            worker,
            "/lHisCoplog/getDataByCondition",
            {
                "json": _json_text(
                    {
                        "_include_lampPostId": [lamp_post_id] if lamp_post_id else [],
                        "_like_lampPostCode": keyword,
                        "_include_lampEffectId": [],
                        "_include_streetId": [],
                        "_timeend_copDate": end_time,
                        "_timebegin_copDate": start_time,
                        "selectSortNum": "1",
                    }
                ),
                "orderBy": "l_his_coplog.cop_date",
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        items = [_normalize_lamp_survey(item) for item in _page_items(payload)]
        return _standard_page_result(
            scope="single_lamp_survey_history",
            filters={
                "keyword": keyword or None,
                "lampPostId": lamp_post_id or None,
                "startTime": start_time,
                "endTime": end_time,
                "rangeSource": range_source,
            },
            page=page,
            size=size,
            payload=payload,
            items=items,
            source_page="单灯节能管理/查询统计/单灯巡测记录",
            source_kind="single_lamp_telemetry_history",
            warnings=[],
        )

    def list_rtu_leakage_alarms(self, worker, arguments: dict) -> dict:
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        payload, metadata = self._rtu_leakage_alarm_page(
            worker,
            arguments,
            page=page,
            size=size,
        )
        page_payload = payload.get("RtuHisHitchAlarm") if isinstance(payload, dict) else payload
        items = [_normalize_rtu_leakage_alarm(item) for item in _page_items(page_payload)]
        return _standard_page_result(
            scope="rtu_branch_leakage_alarms",
            filters=metadata["filters"],
            page=page,
            size=size,
            payload=page_payload,
            items=items,
            source_page="漏电管理/RTU漏电查询/RTU支路漏电报警记录",
            source_kind="rtu_branch_leakage_alarm",
            warnings=_rtu_leakage_warnings(items),
            extra={
                "snapshot": {
                    "todayAlarm": payload.get("todayAlarm") if isinstance(payload, dict) else None,
                    "yesterdayAlarm": payload.get("yesterdayAlarm") if isinstance(payload, dict) else None,
                    "untreated": payload.get("untreated") if isinstance(payload, dict) else None,
                }
            },
        )

    def analyze_rtu_leakage_alarms(self, worker, arguments: dict) -> dict:
        top_n = _bounded_int(arguments.get("top_n"), default=10, minimum=1, maximum=20)
        records: list[dict] = []
        downstream_total = 0
        metadata: dict = {}
        snapshot: dict = {}
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // 100) + 1):
            payload, metadata = self._rtu_leakage_alarm_page(
                worker,
                arguments,
                page=page,
                size=100,
            )
            page_payload = payload.get("RtuHisHitchAlarm") if isinstance(payload, dict) else payload
            page_items = _page_items(page_payload)
            if page == 1:
                downstream_total = _page_total(page_payload)
                if isinstance(payload, dict):
                    snapshot = {
                        "todayAlarm": payload.get("todayAlarm"),
                        "yesterdayAlarm": payload.get("yesterdayAlarm"),
                        "untreated": payload.get("untreated"),
                    }
            records.extend(page_items)
            if (
                not page_items
                or len(records) >= downstream_total
                or len(records) >= _SMARTLIGHT_ANALYSIS_LIMIT
            ):
                break
        items = [_normalize_rtu_leakage_alarm(item) for item in records]
        items.sort(
            key=lambda item: str(item.get("lastActivityAt") or item.get("occurredAt") or ""),
            reverse=True,
        )
        current_count = sum(item.get("stateCode") in {0, 131} for item in items)
        return {
            "scope": "rtu_branch_leakage_analysis",
            "filters": metadata.get("filters", {}),
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "downstreamTotal": downstream_total,
            "retrievedCount": len(records),
            "analyzedCount": len(items),
            "truncated": downstream_total > len(records),
            "analysisLimit": _SMARTLIGHT_ANALYSIS_LIMIT,
            "sourcePage": "漏电管理/RTU漏电查询/RTU支路漏电报警记录",
            "sourceKind": "bounded_rtu_branch_leakage_analysis",
            "currentUnclearedCount": current_count,
            "dailyTrend": _daily_counts(items, "lastActivityAt"),
            "topRtus": _top_counts(items, "rtuName", top_n),
            "topCabinets": _top_counts(items, "cabinet", top_n),
            "topBranches": _top_counts(items, "branch", top_n),
            "snapshot": snapshot,
            "recentEvents": items[:20],
            "warnings": _rtu_leakage_warnings(items),
        }

    def _rtu_leakage_alarm_page(
        self,
        worker,
        arguments: dict,
        *,
        page: int,
        size: int,
    ) -> tuple[Any, dict]:
        context = self._principal_context(worker)
        start_date, end_date, range_source, last_days = _resolve_bounded_date_range(
            arguments,
            default_days=30,
            maximum_days=_SMARTLIGHT_RTU_LEAKAGE_MAX_DAYS,
            range_name="RTU leakage alarm",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        alarm_state = _normalize_choice(
            arguments.get("alarm_state"),
            default="all",
            allowed={"all", "current", "cleared"},
            field_name="alarm_state",
        )
        state_values = {
            "all": ["131", "132"],
            "current": ["131"],
            "cleared": ["132"],
        }[alarm_state]
        payload = self._authorized_post_json(
            worker,
            "/rHisHitchAlarm/getDataByRtuLeakageAlarm",
            {
                "json": _json_text(
                    {
                        "codeOrName": keyword,
                        "_include_conductStatue": state_values,
                        "_include_hitchDicIds": ["htch047"],
                        "_include_weightFacto": [],
                        "_include_isSubmitWorkArea": [],
                        "conductStatue": [],
                        "weightFacto": [1, 2, 3, 4, 5, 6],
                        "hitchDicId": "htch047",
                        "_timebegin_begin": f"{start_date} 00:00:00",
                        "_timeend_end": f"{end_date} 23:59:59",
                        "type": "3",
                        "hideRelayAlarm": "0",
                        "_include_groupId": [],
                        "groupId": "",
                        "dateType": "1",
                        "showData": True,
                        "userId": context["userId"],
                        "leakageCurrent": "",
                        "reporType": "",
                        "workModelIds": [],
                    }
                ),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        return payload, {
            "filters": {
                "keyword": keyword or None,
                "alarmState": alarm_state,
                "startDate": start_date,
                "endDate": end_date,
                "rangeSource": range_source,
                "lastDays": last_days,
                "alarmTypeId": "htch047",
            }
        }

    def list_off_hours_currents(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_time, end_time, range_source = _resolve_bounded_time_range(
            arguments,
            default_hours=24,
            maximum_days=_SMARTLIGHT_SURVEY_MAX_DAYS,
            range_name="off-hours current",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        policy = self._authorized_post_json(
            worker,
            "/rOnoffTime/getOpenCloseTime",
            {"organroleId": context["organroleId"]},
        )
        payload = self._authorized_post_json(
            worker,
            "/rHisCoplogPhase/getOffRelayLeakCurrent",
            {
                "json": _json_text(
                    {
                        "_timebegin_addDateTime": start_time,
                        "_timeend_addDateTime": end_time,
                        "_avgCurrent": 0,
                        "_rating": 0,
                        "rtuCode": keyword,
                    }
                ),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        items = [_normalize_off_hours_current(item) for item in _page_items(payload)]
        return _standard_page_result(
            scope="rtu_off_hours_current_records",
            filters={
                "keyword": keyword or None,
                "startTime": start_time,
                "endTime": end_time,
                "rangeSource": range_source,
            },
            page=page,
            size=size,
            payload=payload,
            items=items,
            source_page="漏电管理/RTU漏电查询/关灯时间段电流",
            source_kind="off_hours_current_observation",
            warnings=[
                "结果仅表示目标页面记录了关灯时段电流，不自动判定漏电、偷电或设备故障。",
                "currentPolicyWindow 仅是查询时的当前策略快照，不用于解释跨日历史记录。",
            ],
            extra={
                "currentPolicyWindow": {
                    "openLightTime": policy.get("openLightTime") if isinstance(policy, dict) else None,
                    "closeLightTime": policy.get("closeLightTime") if isinstance(policy, dict) else None,
                    "semantics": "query_time_snapshot",
                }
            },
        )

    def list_inspection_logs(self, worker, arguments: dict) -> dict:
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_date, end_date, range_source, last_days = _resolve_bounded_date_range(
            arguments,
            default_days=30,
            maximum_days=_SMARTLIGHT_OPERATION_LOG_MAX_DAYS,
            range_name="inspection log",
        )
        plan_id = str(arguments.get("plan_id") or "").strip()
        group = str(arguments.get("group") or "").strip()
        payload = self._authorized_post_json(
            worker,
            "/inspectionLog/getInspectionLog",
            {
                "json": _json_text(
                    {
                        "startTime": start_date,
                        "startTimeTwo": f"{start_date} 00:00:00",
                        "endTime": end_date,
                        "endTimeTwo": f"{end_date} 23:59:59",
                        "planId": plan_id,
                    }
                )
            },
        )
        raw_items = _page_items(payload)
        if group:
            raw_items = [
                item
                for item in raw_items
                if group.casefold() in str(item.get("groupName") or "").casefold()
            ]
        normalized = [_normalize_inspection_log_summary(item) for item in raw_items]
        start = (page - 1) * size
        selected = normalized[start : start + size]
        return {
            "scope": "inspection_log_group_summary",
            "filters": {
                "planId": plan_id or None,
                "group": group or None,
                "startDate": start_date,
                "endDate": end_date,
                "rangeSource": range_source,
                "lastDays": last_days,
                "groupFilterAppliedLocally": bool(group),
            },
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "page": page,
            "size": size,
            "downstreamTotal": len(_page_items(payload)),
            "returnedCount": len(selected),
            "count": len(selected),
            "truncated": False,
            "sourcePage": "巡检管理/数据统计/巡检日志",
            "sourceKind": "inspection_group_statistics",
            "warnings": [
                "该页面返回巡检组统计，不是逐人、逐设备的打卡明细；不会据此推断具体巡检人员或设备。"
            ],
            "items": selected,
        }

    def list_maintenance_records(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        start_date, end_date, range_source, last_days = _resolve_bounded_date_range(
            arguments,
            default_days=30,
            maximum_days=_SMARTLIGHT_OPERATION_LOG_MAX_DAYS,
            range_name="maintenance record",
        )
        keyword = str(arguments.get("keyword") or "").strip()
        overhaul_user = str(arguments.get("overhaul_user") or "").strip()
        device_type = _normalize_choice(
            arguments.get("device_type"),
            default="all",
            allowed={"all", "rtu", "lamppost"},
            field_name="device_type",
        )
        local_filter = device_type != "all"
        query = {
            "_deviceCode": keyword,
            "_startTime": start_date,
            "_endTime": end_date,
            "_overhaulUser": overhaul_user,
        }
        raw_items: list[dict] = []
        downstream_total = 0
        if local_filter:
            for downstream_page in range(
                1,
                (_SMARTLIGHT_ANALYSIS_LIMIT // _SMARTLIGHT_ANALYSIS_PAGE_SIZE) + 1,
            ):
                payload = self._authorized_post_json(
                    worker,
                    "/inspectionOverhaul/getDataByCondition",
                    {
                        "json": _json_text(query),
                        "organroleId": context["organroleId"],
                        "pageNum": downstream_page,
                        "pageSize": _SMARTLIGHT_ANALYSIS_PAGE_SIZE,
                    },
                )
                page_items = _page_items(payload)
                if downstream_page == 1:
                    downstream_total = _page_total(payload)
                raw_items.extend(page_items)
                if (
                    not page_items
                    or len(raw_items) >= downstream_total
                    or len(raw_items) >= _SMARTLIGHT_ANALYSIS_LIMIT
                ):
                    break
        else:
            payload = self._authorized_post_json(
                worker,
                "/inspectionOverhaul/getDataByCondition",
                {
                    "json": _json_text(query),
                    "organroleId": context["organroleId"],
                    "pageNum": page,
                    "pageSize": size,
                },
            )
            raw_items = _page_items(payload)
            downstream_total = _page_total(payload)
        items = [_normalize_maintenance_record(item) for item in raw_items]
        if local_filter:
            items = [item for item in items if item.get("deviceType") == device_type]
            start = (page - 1) * size
            selected = items[start : start + size]
            total = len(items)
        else:
            selected = items
            total = _page_total(payload)
        return {
            "scope": "inspection_maintenance_records",
            "filters": {
                "keyword": keyword or None,
                "overhaulUser": overhaul_user or None,
                "deviceType": device_type,
                "startDate": start_date,
                "endDate": end_date,
                "rangeSource": range_source,
                "lastDays": last_days,
                "deviceTypeFilterAppliedLocally": local_filter,
            },
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            "page": page,
            "size": size,
            "downstreamTotal": downstream_total,
            "total": total,
            "returnedCount": len(selected),
            "count": len(selected),
            "truncated": local_filter and downstream_total > len(raw_items),
            "sourcePage": "巡检管理/数据统计/检修记录",
            "sourceKind": "inspection_maintenance_record",
            "warnings": [
                "页面详情只按设备标识读取当前可见检查项，没有稳定传递检修记录 ID，因此本期不开放统一记录详情工具。"
            ],
            "items": selected,
        }

    def list_lampposts(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        keyword = str(arguments.get("keyword") or "").strip()
        payload = self._authorized_post_json(
            worker,
            "/lLamppost/getDataByConditionForFacilityEx",
            {
                "json": _json_text(
                    _lamppost_filters(context, keyword)
                ),
                "orderBy": "lamp_post_code",
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        items = _page_items(payload)
        return {
            "keyword": keyword or None,
            "page": page,
            "size": size,
            "total": _page_total(payload),
            "count": len(items),
            "items": [_normalize_lamppost(item) for item in items],
        }

    def search_assets(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        asset_type = _normalize_asset_type(arguments.get("asset_type"))
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        keyword = str(arguments.get("keyword") or "").strip()
        payload = self._asset_page(
            worker,
            context,
            asset_type=asset_type,
            keyword=keyword,
            page=page,
            size=size,
        )
        items = _page_items(payload)
        return {
            "assetType": asset_type,
            "keyword": keyword or None,
            "page": page,
            "size": size,
            "total": _page_total(payload),
            "count": len(items),
            "items": [
                _normalize_asset_summary(asset_type, item)
                for item in items
            ],
        }

    def asset_detail(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        asset_type = _normalize_asset_type(arguments.get("asset_type"))
        asset_id = str(arguments.get("asset_id") or "").strip()
        if not asset_id:
            raise ValueError("asset_id is required")

        if asset_type == "lamppost":
            payload = self._authorized_post_json(
                worker,
                "/lLamppost/getLampPostDetail",
                {
                    "lampPostId": asset_id,
                    "userId": context["userId"],
                },
            )
            record = _result_record(payload)
            if record is None or str(
                _first(record, "lampPostID", "lampPostId", "id") or ""
            ) != asset_id:
                return _missing_asset_detail(asset_type, asset_id)
            return {
                "assetType": asset_type,
                "assetId": asset_id,
                "found": True,
                "detail": _normalize_lamppost_detail(record),
            }

        record = self._find_asset_record(
            worker,
            context,
            asset_type=asset_type,
            asset_id=asset_id,
        )
        if record is None:
            return _missing_asset_detail(asset_type, asset_id)
        result = {
            "assetType": asset_type,
            "assetId": asset_id,
            "found": True,
            "detail": (
                _normalize_cabinet_detail(record)
                if asset_type == "cabinet"
                else _normalize_rtu_detail(record)
            ),
        }
        if asset_type == "rtu":
            relays_payload = self._authorized_post_json(
                worker,
                "/rRturelay/getDataByCondition",
                {
                    "json": _json_text({"rtuId": asset_id}),
                    "pageNum": 1,
                    "pageSize": 999,
                    "organroleId": context["organroleId"],
                },
            )
            relay_items = _page_items(relays_payload)
            result["relayTotal"] = _page_total(relays_payload)
            result["relays"] = [_normalize_rtu_relay(item) for item in relay_items]
        return result

    def _find_asset_record(
        self,
        worker,
        context: dict,
        *,
        asset_type: str,
        asset_id: str,
    ) -> dict | None:
        for page in range(1, 6):
            payload = self._asset_page(
                worker,
                context,
                asset_type=asset_type,
                keyword="",
                page=page,
                size=100,
                asset_id=asset_id,
            )
            items = _page_items(payload)
            for item in items:
                if str(_asset_id(asset_type, item) or "") == asset_id:
                    return item
            if not items or page * 100 >= _page_total(payload):
                break
        return None

    def _asset_page(
        self,
        worker,
        context: dict,
        *,
        asset_type: str,
        keyword: str,
        page: int,
        size: int,
        asset_id: str = "",
    ) -> Any:
        if asset_type == "lamppost":
            query = _lamppost_filters(context, keyword)
            if asset_id:
                query["lampPostId"] = asset_id
            return self._authorized_post_json(
                worker,
                "/lLamppost/getDataByConditionForFacilityEx",
                {
                    "json": _json_text(query),
                    "orderBy": "lamp_post_code",
                    "pageNum": page,
                    "pageSize": size,
                    "organroleId": context["organroleId"],
                },
            )
        if asset_type == "rtu":
            return self._authorized_post_json(
                worker,
                "/rRtu/getDataByCondition",
                {
                    "json": _json_text(
                        {
                            "_like_params": keyword,
                            "_timebegin_addDate": "",
                            "_timeend_addDate": "",
                            "rtuId": asset_id,
                        }
                    ),
                    "orderBy": "add_date desc",
                    "pageNum": page,
                    "pageSize": size,
                    "organroleId": context["organroleId"],
                },
            )
        return self._authorized_post_json(
            worker,
            "/rControlCabinet/getDataByCondition",
            {
                "json": _json_text(
                    {
                        "labels": [],
                        "_like_controlCabinetName": keyword,
                        "_timebegin_addDate": "",
                        "_timeend_addDate": "",
                        "_include_transTypeId": [],
                        "_include_workAreaId": [],
                        "ltOrGtTime": "",
                        "_timeend_maxDate": "",
                        "controlCabinetName": "",
                        "controlCabinetId": asset_id,
                        "controlCabinetCode": "",
                        "userId": context["userId"],
                        "electricalType": "",
                        "electricityNumberFlag": "",
                    }
                ),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )

    def list_alarms(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        keyword = str(arguments.get("keyword") or "").strip()
        sort_by = _normalize_choice(
            arguments.get("sort_by"),
            default="occurred_at",
            allowed=set(_SMARTLIGHT_ALARM_SORTS),
            field_name="sort_by",
        )
        sort_field, date_type = _SMARTLIGHT_ALARM_SORTS[sort_by]
        payload = self._alarm_list_page(
            worker,
            context,
            keyword=keyword,
            date_type=date_type,
            page=page,
            size=size,
        )
        page_payload = payload.get("RtuHisHitchAlarm") if isinstance(payload, dict) else payload
        items = _page_items(page_payload)
        normalized_items = [_normalize_alarm(item) for item in items]
        if not _smartlight_alarm_order_verified(normalized_items, sort_field):
            raise SmartlightSessionCheckUnavailable(
                "照明告警接口没有按请求的时间字段返回全局倒序结果。"
            )
        normalized_items = _smartlight_stabilize_alarm_order(
            normalized_items,
            sort_field,
        )
        latest_group = None
        if page == 1:
            tie_items = normalized_items
            if size < _SMARTLIGHT_ALARM_TIE_PROBE_SIZE and _page_total(page_payload) > size:
                tie_payload = self._alarm_list_page(
                    worker,
                    context,
                    keyword=keyword,
                    date_type=date_type,
                    page=1,
                    size=_SMARTLIGHT_ALARM_TIE_PROBE_SIZE,
                )
                tie_page = (
                    tie_payload.get("RtuHisHitchAlarm")
                    if isinstance(tie_payload, dict)
                    else tie_payload
                )
                tie_items = [_normalize_alarm(item) for item in _page_items(tie_page)]
                if not _smartlight_alarm_order_verified(tie_items, sort_field):
                    raise SmartlightSessionCheckUnavailable(
                        "照明告警接口的并列时间探针未保持全局倒序。"
                    )
                tie_items = _smartlight_stabilize_alarm_order(
                    tie_items,
                    sort_field,
                )
            latest_group = _smartlight_latest_alarm_group(
                tie_items,
                total=_page_total(page_payload),
                sort_field=sort_field,
            )
            normalized_items = tie_items[:size]
        return {
            "alarmSource": "rtu",
            "keyword": keyword or None,
            "sortBy": sort_by,
            "page": page,
            "size": size,
            "total": _page_total(page_payload),
            "count": len(items),
            "summary": _selected_fields(
                payload,
                ("todayAlarm", "untreated", "yesterdayAlarm"),
            ),
            "summaryScope": {
                "type": "current_system_snapshot",
                "pageFiltered": False,
                "checkedAt": datetime.now(timezone.utc).isoformat(),
            },
            "sort": {
                "field": sort_field,
                "direction": "desc",
                "scope": "downstream_global",
                "verified": True,
                "downstreamParameter": {"dateType": date_type},
                "tieBreakers": [
                    (
                        "lastActivityAt"
                        if sort_field == "occurredAt"
                        else "occurredAt"
                    ),
                    "id",
                ],
            },
            "timeSemantics": {
                "defaultRecentField": "occurredAt",
                "selectedField": sort_field,
                "occurredAt": "Time when the alarm first occurred.",
                "lastActivityAt": "Most recent alarm activity.",
            },
            "latestGroup": latest_group,
            "items": normalized_items,
        }

    def _alarm_list_page(
        self,
        worker,
        context: dict,
        *,
        keyword: str,
        date_type: str,
        page: int,
        size: int,
    ) -> Any:
        return self._authorized_post_json(
            worker,
            "/rHisHitchAlarm/getDataByRtuAlarm",
            {
                "json": _json_text(
                    {
                        "codeOrName": keyword,
                        "dateType": date_type,
                        "organroleId": context["organroleId"],
                    }
                ),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )

    def read_alarm_remark(self, worker, arguments: dict) -> dict:
        alarm_id = _normalize_smartlight_alarm_read_id(arguments)
        record = self.get_alarm_remark(worker, alarm_id)
        remark = _smartlight_remark_text(record)
        return {
            "alarmId": alarm_id,
            "remark": remark,
            "hasRemark": bool(remark),
            "createUser": (
                str(record.get("createUser") or "").strip() or None
                if isinstance(record, dict)
                else None
            ),
            "createTime": (
                record.get("createTime") if isinstance(record, dict) else None
            ),
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }

    def analyze_alarms(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        keyword = str(arguments.get("keyword") or "").strip()
        alarm_type = str(arguments.get("alarm_type") or "").strip()
        alarm_state = _normalize_choice(
            arguments.get("alarm_state"),
            default="all",
            allowed={"all", "current", "cleared"},
            field_name="alarm_state",
        )
        time_field = _normalize_choice(
            arguments.get("time_field"),
            default="last_activity",
            allowed={"last_activity", "occurred"},
            field_name="time_field",
        )
        top_n = _bounded_int(arguments.get("top_n"), default=10, minimum=1, maximum=20)
        start_date, end_date, range_source, last_days = _resolve_analysis_date_range(
            arguments
        )
        raw_items: list[dict] = []
        downstream_total = 0
        snapshot: dict[str, Any] = {}
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // _SMARTLIGHT_ANALYSIS_PAGE_SIZE) + 1):
            payload = self._alarm_analysis_page(
                worker,
                context,
                keyword=keyword,
                alarm_state=alarm_state,
                time_field=time_field,
                start_date=start_date,
                end_date=end_date,
                page=page,
            )
            if page == 1:
                snapshot = _selected_fields(
                    payload,
                    ("todayAlarm", "untreated", "yesterdayAlarm"),
                )
            page_payload = (
                payload.get("RtuHisHitchAlarm")
                if isinstance(payload, dict)
                else payload
            )
            page_items = _page_items(page_payload)
            if page == 1:
                downstream_total = _page_total(page_payload)
            raw_items.extend(page_items)
            if (
                not page_items
                or len(raw_items) >= downstream_total
                or len(raw_items) >= _SMARTLIGHT_ANALYSIS_LIMIT
            ):
                break

        normalized = [_normalize_alarm(item) for item in raw_items]
        if alarm_type:
            needle = alarm_type.casefold()
            normalized = [
                item
                for item in normalized
                if needle in str(item.get("type") or "").casefold()
            ]
        if alarm_state != "all":
            expected_code = 0 if alarm_state == "current" else 1
            normalized = [
                item for item in normalized if item.get("stateCode") == expected_code
            ]
        sort_field = "lastActivityAt" if time_field == "last_activity" else "occurredAt"
        normalized.sort(
            key=lambda item: str(item.get(sort_field) or ""),
            reverse=True,
        )
        result = {
            "alarmSource": "rtu",
            "filters": {
                "keyword": keyword or None,
                "alarmType": alarm_type or None,
                "alarmState": alarm_state,
                "timeField": time_field,
            },
            "dateRange": {
                "source": range_source,
                "lastDays": last_days,
                "startDate": start_date,
                "endDate": end_date,
                "inclusive": True,
                "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
            },
            "downstreamTotal": downstream_total,
            "retrievedCount": len(raw_items),
            "analyzedCount": len(normalized),
            "truncated": downstream_total > len(raw_items),
            "analysisLimit": _SMARTLIGHT_ANALYSIS_LIMIT,
            "snapshot": snapshot,
            "stateCounts": _top_counts(normalized, "stateLabel", top_n),
            "topAlarmTypes": _top_counts(normalized, "type", top_n),
            "topDevices": _top_counts(normalized, "device", top_n),
            "dailyTrend": _daily_counts(normalized, sort_field),
            "recentRecords": normalized[:20],
            "timeSemantics": {
                "filterField": sort_field,
                "occurredAt": "Time when the alarm first occurred.",
                "lastActivityAt": "Most recent alarm activity.",
            },
        }
        if arguments.get("_include_report_rows") is True:
            result["_reportRows"] = normalized
        return result

    def _alarm_analysis_page(
        self,
        worker,
        context: dict,
        *,
        keyword: str,
        alarm_state: str,
        time_field: str,
        start_date: str,
        end_date: str,
        page: int,
    ) -> Any:
        state_values = {
            "all": ["131", "132"],
            "current": ["131"],
            "cleared": ["132"],
        }[alarm_state]
        return self._authorized_post_json(
            worker,
            "/rHisHitchAlarm/getDataByRtuAlarm",
            {
                "json": _json_text(
                    {
                        "codeOrName": keyword,
                        "_include_conductStatue": state_values,
                        "_include_hitchDicIds": [],
                        "_include_weightFacto": [],
                        "_include_isSubmitWorkArea": [],
                        "conductStatue": [],
                        "weightFacto": [1, 2, 3, 4, 5, 6],
                        "hitchDicId": "",
                        "_timebegin_begin": (
                            f"{start_date} 00:00:00" if start_date else ""
                        ),
                        "_timeend_end": (
                            f"{end_date} 23:59:59" if end_date else ""
                        ),
                        "type": "3",
                        "_include_groupId": [],
                        "groupId": "",
                        "dateType": "1" if time_field == "last_activity" else "0",
                        "showData": True,
                        "userId": context["userId"],
                        "leakageCurrent": "",
                        "reporType": "",
                    }
                ),
                "pageNum": page,
                "pageSize": _SMARTLIGHT_ANALYSIS_PAGE_SIZE,
                "organroleId": context["organroleId"],
            },
        )

    def list_inspection_tasks(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)
        task_name = str(arguments.get("task_name") or "").strip()
        plan_name = str(arguments.get("plan_name") or "").strip()
        state = _normalize_inspection_state_filter(arguments.get("state"))
        payload = self._authorized_post_json(
            worker,
            "/inspectionTask/getDataByCondition",
            {
                "json": _json_text(
                    {
                        "_taskName": task_name,
                        "_planName": plan_name,
                        "_taskState": state,
                    }
                ),
                "pageNum": page,
                "pageSize": size,
                "organroleId": context["organroleId"],
            },
        )
        items = _page_items(payload)
        return {
            "filters": {
                "taskName": task_name or None,
                "planName": plan_name or None,
                "state": state,
            },
            "page": page,
            "size": size,
            "total": _page_total(payload),
            "count": len(items),
            "fieldSemantics": {
                "progress": (
                    "Downstream-reported task progress. Display it exactly and do not "
                    "derive another percentage from device counts."
                ),
                "deviceCounts": (
                    "Independent confirmed, lamp-post and RTU counts. Do not present "
                    "confirmed/(lampPosts+rtus) as completed/total."
                ),
            },
            "items": [_normalize_inspection_task(item) for item in items],
        }

    def inspection_task_detail(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        start_date = _optional_date(arguments.get("start_date"), "start_date")
        end_date = _optional_date(arguments.get("end_date"), "end_date")
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        detail_date = _optional_date(arguments.get("detail_date"), "detail_date")
        clockin_user = str(arguments.get("clockin_user") or "").strip()
        has_issues = arguments.get("has_issues")
        if has_issues is not None and not isinstance(has_issues, bool):
            raise ValueError("has_issues must be a boolean")
        page = _bounded_int(arguments.get("page"), default=1, minimum=1, maximum=10000)
        size = _bounded_int(arguments.get("size"), default=20, minimum=1, maximum=100)

        task_payload = self._authorized_post_json(
            worker,
            "/inspectionTask/getDataByCondition",
            {
                "json": _json_text({"_taskId": task_id}),
                "pageNum": 1,
                "pageSize": 100,
                "organroleId": context["organroleId"],
            },
        )
        task_record = next(
            (
                item
                for item in _page_items(task_payload)
                if str(_first(item, "inspectionTaskId", "taskId", "id") or "")
                == task_id
            ),
            None,
        )
        daily_payload = self._authorized_post_json(
            worker,
            "/InspectionDeviceGroup/getDataByCondition",
            {
                "json": _json_text(
                    {
                        "taskId": task_id,
                        "startTime": start_date,
                        "endTime": end_date,
                    }
                ),
                "pageNum": 1,
                "pageSize": _SMARTLIGHT_ANALYSIS_LIMIT,
            },
        )
        daily_records = [_normalize_inspection_day(item) for item in _page_items(daily_payload)]
        result: dict[str, Any] = {
            "taskId": task_id,
            "found": task_record is not None or bool(daily_records),
            "task": _normalize_inspection_task(task_record) if task_record else None,
            "filters": {
                "startDate": start_date or None,
                "endDate": end_date or None,
                "detailDate": detail_date or None,
                "clockinUser": clockin_user or None,
                "hasIssues": has_issues,
            },
            "dailyTotal": _page_total(daily_payload),
            "dailyCount": len(daily_records),
            "dailyTruncated": _page_total(daily_payload) > len(daily_records),
            "days": daily_records,
            "fieldSemantics": {
                "plannedDeviceCount": "Downstream planned device count for that day.",
                "completedDeviceCount": "Downstream completed device count for that day.",
                "completionRate": "Downstream-reported daily completion rate.",
                "missingDevices": "Not available; do not infer device identities from a count difference.",
            },
        }
        if not detail_date:
            return result

        matching_day = next(
            (item for item in daily_records if item.get("date") == detail_date),
            None,
        )
        if matching_day is None:
            result.update(
                {
                    "detailDateFound": False,
                    "clockinTotal": 0,
                    "clockinCount": 0,
                    "clockins": [],
                }
            )
            return result
        clockin_payload = self._authorized_post_json(
            worker,
            "/inspectionTask/getClockinDataByTaskId",
            {
                "json": _json_text(
                    {
                        "taskId": task_id,
                        "groupId": matching_day.get("groupId"),
                        "clockinUser": clockin_user,
                        "hasIssues": has_issues,
                        "_startTime": f"{detail_date} 00:00:00",
                        "_endTime": f"{detail_date} 24:00:00",
                    }
                ),
                "pageNum": page,
                "pageSize": size,
            },
        )
        clockins = [_normalize_inspection_clockin(item) for item in _page_items(clockin_payload)]
        result.update(
            {
                "detailDateFound": True,
                "clockinPage": page,
                "clockinSize": size,
                "clockinTotal": _page_total(clockin_payload),
                "clockinCount": len(clockins),
                "clockins": clockins,
            }
        )
        return result

    def leakage_summary(self, worker, arguments: dict) -> dict:
        result = self.list_lamp_alarms(worker, arguments)
        result.update(
            {
                "deprecated": True,
                "canonicalTool": "smartlight_lamp_alarm_list",
                "alarmSource": "single_lamp",
                "compatibilityNotice": (
                    "兼容旧入口：该接口实际返回单灯告警，不代表漏电记录。"
                ),
            }
        )
        return result

    def analyze_leakage(self, worker, arguments: dict) -> dict:
        result = self.analyze_lamp_alarms(worker, arguments)
        result.update(
            {
                "deprecated": True,
                "canonicalTool": "smartlight_lamp_alarm_analysis",
                "canonicalReportType": "lamp_alarm_analysis",
                "alarmSource": "single_lamp",
                "compatibilityNotice": (
                    "兼容旧入口：该接口实际分析单灯告警，不代表漏电分析。"
                ),
            }
        )
        return result

    def export_report(self, worker, arguments: dict) -> dict:
        report_type = _normalize_choice(
            arguments.get("report_type"),
            default="",
            allowed={
                "alarm_analysis",
                "lamp_alarm_analysis",
                "leakage_analysis",
                "asset_inventory",
                "inspection_progress",
                "energy_records",
                "energy_analysis",
                "lamp_survey_records",
                "rtu_leakage_alarms",
                "rtu_leakage_analysis",
                "inspection_logs",
                "maintenance_records",
            },
            field_name="report_type",
        )
        report_arguments = {
            key: value
            for key, value in arguments.items()
            if key != "report_type" and value is not None
        }
        if report_type == "alarm_analysis":
            analysis = self.analyze_alarms(
                worker,
                {**report_arguments, "_include_report_rows": True},
            )
            rows = analysis.pop("_reportRows")
            return _smartlight_report_result(
                report_type=report_type,
                title="照明RTU告警分析",
                columns=(
                    ("id", "告警ID"),
                    ("occurredAt", "首次发生时间"),
                    ("lastActivityAt", "最近活动时间"),
                    ("device", "设备"),
                    ("deviceCode", "设备编号"),
                    ("type", "告警类型"),
                    ("level", "告警级别"),
                    ("stateLabel", "状态"),
                    ("message", "告警内容"),
                    ("workArea", "工区"),
                    ("group", "分组"),
                ),
                rows=rows,
                metadata={
                    "filters": analysis["filters"],
                    "dateRange": analysis["dateRange"],
                    "downstreamTotal": analysis["downstreamTotal"],
                    "retrievedCount": analysis["retrievedCount"],
                    "truncated": analysis["truncated"],
                },
            )
        if report_type in {"lamp_alarm_analysis", "leakage_analysis"}:
            analysis = self.analyze_lamp_alarms(
                worker,
                {**report_arguments, "_include_report_rows": True},
            )
            rows = analysis.pop("_reportRows")
            return _smartlight_report_result(
                report_type=report_type,
                title="照明单灯告警分析",
                columns=(
                    ("id", "告警ID"),
                    ("occurredAt", "首次发生时间"),
                    ("lastActivityAt", "最近活动时间"),
                    ("lampPost", "灯杆"),
                    ("lamp", "灯具"),
                    ("road", "道路"),
                    ("cabinet", "控制柜"),
                    ("alarmType", "告警类型"),
                    ("stateLabel", "状态"),
                ),
                rows=rows,
                metadata={
                    "dateRange": analysis["dateRange"],
                    "downstreamTotal": analysis["downstreamTotal"],
                    "retrievedCount": analysis["retrievedCount"],
                    "truncated": analysis["truncated"],
                    "alarmSource": "single_lamp",
                    "deprecated": report_type == "leakage_analysis",
                    "canonicalReportType": "lamp_alarm_analysis",
                },
            )
        if report_type == "energy_records":
            rows, metadata = self._collect_report_rows(
                self.list_energy_records,
                worker,
                report_arguments,
            )
            flat_rows = [
                {
                    "deviceId": row.get("deviceId"),
                    "deviceCode": row.get("deviceCode"),
                    "deviceName": row.get("deviceName"),
                    "productModel": row.get("productModel"),
                    "date": point.get("date"),
                    "value": point.get("value"),
                    "unit": row.get("unit"),
                }
                for row in rows
                for point in row.get("periodValues") or []
            ]
            return _smartlight_report_result(
                report_type=report_type,
                title="照明RTU用电记录",
                columns=(
                    ("deviceId", "RTU ID"),
                    ("deviceCode", "RTU编号"),
                    ("deviceName", "RTU名称"),
                    ("productModel", "产品型号"),
                    ("date", "日期"),
                    ("value", "页面用电值"),
                    ("unit", "单位"),
                ),
                rows=flat_rows,
                metadata=metadata,
            )
        if report_type == "energy_analysis":
            analysis = self.analyze_energy(worker, report_arguments)
            return _smartlight_report_result(
                report_type=report_type,
                title="照明RTU用电分析",
                columns=(
                    ("date", "日期"),
                    ("value", "合计值"),
                    ("dataPointCount", "有效数据点"),
                    ("unit", "单位"),
                ),
                rows=analysis["dailyTrend"],
                metadata={
                    "filters": analysis["filters"],
                    "downstreamTotal": analysis["downstreamTotal"],
                    "retrievedCount": analysis["retrievedCount"],
                    "truncated": analysis["truncated"],
                    "unit": analysis["unit"],
                    "warnings": analysis["warnings"],
                    "topDevices": analysis["topDevices"],
                },
            )
        if report_type == "lamp_survey_records":
            rows, metadata = self._collect_report_rows(
                self.list_lamp_survey_records,
                worker,
                report_arguments,
            )
            return _smartlight_report_result(
                report_type=report_type,
                title="照明单灯巡测记录",
                columns=(
                    ("id", "记录ID"),
                    ("receivedAt", "平台接收时间"),
                    ("deviceTime", "设备时间"),
                    ("lampPostCode", "灯杆编号"),
                    ("lampPostName", "灯杆名称"),
                    ("lampName", "灯具"),
                    ("road", "道路"),
                    ("cabinet", "控制箱"),
                    ("voltage", "电压"),
                    ("current", "电流"),
                    ("activePower", "有功功率"),
                    ("powerFactor", "功率因数"),
                    ("alarmContent", "异常内容"),
                ),
                rows=rows,
                metadata=metadata,
            )
        if report_type in {"rtu_leakage_alarms", "rtu_leakage_analysis"}:
            rows, metadata = self._collect_report_rows(
                self.list_rtu_leakage_alarms,
                worker,
                report_arguments,
            )
            metadata["reportSemantics"] = (
                "bounded_analysis_source_rows"
                if report_type == "rtu_leakage_analysis"
                else "alarm_rows"
            )
            return _smartlight_report_result(
                report_type=report_type,
                title=(
                    "照明RTU支路漏电分析明细"
                    if report_type == "rtu_leakage_analysis"
                    else "照明RTU支路漏电报警"
                ),
                columns=(
                    ("id", "告警ID"),
                    ("occurredAt", "首次发生时间"),
                    ("lastActivityAt", "最近活动时间"),
                    ("rtuCode", "RTU编号"),
                    ("rtuName", "RTU名称"),
                    ("cabinet", "控制箱"),
                    ("branch", "支路"),
                    ("leakageCurrent", "漏电电流"),
                    ("leakageUnit", "单位"),
                    ("threshold", "阈值"),
                    ("stateLabel", "状态"),
                    ("remark", "备注"),
                ),
                rows=rows,
                metadata=metadata,
            )
        if report_type == "inspection_logs":
            rows, metadata = self._collect_report_rows(
                self.list_inspection_logs,
                worker,
                report_arguments,
            )
            return _smartlight_report_result(
                report_type=report_type,
                title="照明巡检日志统计",
                columns=(
                    ("groupId", "巡检组ID"),
                    ("groupName", "巡检组"),
                    ("shouldChecked", "应巡数量"),
                    ("actuallyChecked", "实巡数量"),
                    ("normal", "正常数量"),
                    ("abnormal", "异常数量"),
                ),
                rows=rows,
                metadata=metadata,
            )
        if report_type == "maintenance_records":
            rows, metadata = self._collect_report_rows(
                self.list_maintenance_records,
                worker,
                report_arguments,
            )
            return _smartlight_report_result(
                report_type=report_type,
                title="照明检修记录",
                columns=(
                    ("recordId", "检修记录ID"),
                    ("overhauledAt", "检修时间"),
                    ("overhaulUserName", "检修人员"),
                    ("deviceType", "设备类型"),
                    ("deviceCode", "设备编号"),
                    ("road", "道路"),
                ),
                rows=rows,
                metadata=metadata,
            )
        if report_type == "asset_inventory":
            return self._export_asset_inventory(worker, report_arguments)
        return self._export_inspection_progress(worker, report_arguments)

    def _collect_report_rows(self, method, worker, arguments: dict) -> tuple[list[dict], dict]:
        rows: list[dict] = []
        first_result: dict | None = None
        fixed_arguments = dict(arguments)
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // 100) + 1):
            result = method(worker, {**fixed_arguments, "page": page, "size": 100})
            if first_result is None:
                first_result = result
                filters = result.get("filters") if isinstance(result, dict) else None
                if isinstance(filters, dict):
                    for input_name, filter_name in (
                        ("start_date", "startDate"),
                        ("end_date", "endDate"),
                        ("start_time", "startTime"),
                        ("end_time", "endTime"),
                    ):
                        if filters.get(filter_name):
                            fixed_arguments[input_name] = filters[filter_name]
                    fixed_arguments.pop("last_days", None)
            page_rows = result.get("items") if isinstance(result, dict) else None
            if not isinstance(page_rows, list):
                page_rows = []
            rows.extend(item for item in page_rows if isinstance(item, dict))
            total = int(result.get("total") or result.get("downstreamTotal") or 0)
            if not page_rows or len(rows) >= total or len(rows) >= _SMARTLIGHT_ANALYSIS_LIMIT:
                break
        first_result = first_result or {}
        downstream_total = int(first_result.get("downstreamTotal") or 0)
        return rows[:_SMARTLIGHT_ANALYSIS_LIMIT], {
            "filters": first_result.get("filters") or {},
            "timezone": first_result.get("timezone"),
            "sourcePage": first_result.get("sourcePage"),
            "sourceKind": first_result.get("sourceKind"),
            "warnings": first_result.get("warnings") or [],
            "downstreamTotal": downstream_total,
            "retrievedCount": len(rows),
            "truncated": downstream_total > len(rows),
        }

    def _export_asset_inventory(self, worker, arguments: dict) -> dict:
        context = self._principal_context(worker)
        asset_type = _normalize_asset_type(arguments.get("asset_type"))
        keyword = str(arguments.get("keyword") or "").strip()
        rows: list[dict] = []
        downstream_total = 0
        for page in range(1, (_SMARTLIGHT_ANALYSIS_LIMIT // 100) + 1):
            payload = self._asset_page(
                worker,
                context,
                asset_type=asset_type,
                keyword=keyword,
                page=page,
                size=100,
            )
            items = _page_items(payload)
            if page == 1:
                downstream_total = _page_total(payload)
            rows.extend(_normalize_asset_summary(asset_type, item) for item in items)
            if (
                not items
                or len(rows) >= downstream_total
                or len(rows) >= _SMARTLIGHT_ANALYSIS_LIMIT
            ):
                break
        labels = {"cabinet": "控制柜", "rtu": "RTU", "lamppost": "灯杆"}
        columns = {
            "cabinet": (
                ("id", "设施ID"),
                ("code", "编号"),
                ("name", "名称"),
                ("state", "状态"),
                ("road", "道路"),
                ("side", "道路侧"),
                ("workArea", "工区"),
                ("address", "地址"),
                ("capacity", "容量"),
                ("electricalType", "供电类型"),
            ),
            "rtu": (
                ("id", "设施ID"),
                ("code", "编号"),
                ("name", "名称"),
                ("model", "型号"),
                ("type", "类型"),
                ("cabinet", "控制柜"),
                ("group", "分组"),
                ("workArea", "工区"),
                ("state", "状态"),
            ),
            "lamppost": (
                ("id", "设施ID"),
                ("code", "编号"),
                ("name", "名称"),
                ("road", "道路"),
                ("cabinet", "控制柜"),
                ("workArea", "工区"),
                ("lampCount", "灯具数量"),
                ("state", "状态"),
            ),
        }[asset_type]
        return _smartlight_report_result(
            report_type="asset_inventory",
            title=f"照明{labels[asset_type]}清单",
            columns=columns,
            rows=rows,
            metadata={
                "assetType": asset_type,
                "keyword": keyword or None,
                "downstreamTotal": downstream_total,
                "retrievedCount": len(rows),
                "truncated": downstream_total > len(rows),
            },
        )

    def _export_inspection_progress(self, worker, arguments: dict) -> dict:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required for inspection_progress")
        detail_arguments = {
            key: value
            for key, value in arguments.items()
            if key
            in {
                "task_id",
                "start_date",
                "end_date",
                "detail_date",
                "clockin_user",
                "has_issues",
            }
        }
        detail = self.inspection_task_detail(
            worker,
            {**detail_arguments, "page": 1, "size": 100},
        )
        detail_date = detail["filters"].get("detailDate")
        if not detail_date:
            return _smartlight_report_result(
                report_type="inspection_progress",
                title="照明巡检进度",
                columns=(
                    ("date", "日期"),
                    ("plannedDeviceCount", "计划设备数"),
                    ("completedDeviceCount", "完成设备数"),
                    ("completionRate", "下游完成率"),
                ),
                rows=detail["days"],
                metadata={
                    "taskId": task_id,
                    "task": detail.get("task"),
                    "filters": detail["filters"],
                    "downstreamTotal": detail["dailyTotal"],
                    "retrievedCount": detail["dailyCount"],
                    "truncated": detail["dailyTruncated"],
                },
            )

        rows = list(detail.get("clockins") or [])
        downstream_total = int(detail.get("clockinTotal") or 0)
        for page in range(2, (_SMARTLIGHT_ANALYSIS_LIMIT // 100) + 1):
            if len(rows) >= downstream_total or len(rows) >= _SMARTLIGHT_ANALYSIS_LIMIT:
                break
            page_detail = self.inspection_task_detail(
                worker,
                {**detail_arguments, "page": page, "size": 100},
            )
            page_rows = page_detail.get("clockins") or []
            rows.extend(page_rows)
            if not page_rows:
                break
        rows = rows[:_SMARTLIGHT_ANALYSIS_LIMIT]
        return _smartlight_report_result(
            report_type="inspection_progress",
            title=f"照明巡检打卡明细_{detail_date}",
            columns=(
                ("id", "打卡ID"),
                ("recordedAt", "打卡时间"),
                ("recorder", "打卡人"),
                ("deviceId", "设施ID"),
                ("deviceCode", "设施编号"),
                ("deviceName", "设施名称"),
                ("deviceType", "设施类型"),
                ("position", "位置"),
                ("hasIssues", "是否发现问题"),
                ("issueDescription", "问题描述"),
            ),
            rows=rows,
            metadata={
                "taskId": task_id,
                "task": detail.get("task"),
                "filters": detail["filters"],
                "downstreamTotal": downstream_total,
                "retrievedCount": len(rows),
                "truncated": downstream_total > len(rows),
            },
        )

    def alarm_remark_snapshot(self, worker, alarm_id: str) -> dict:
        record = self._find_alarm_record(worker, alarm_id)
        rtu_id = str(record.get("rtuId") or "").strip()
        if not rtu_id:
            raise SmartlightAlarmRemarkContractMismatch(
                "目标告警缺少 RTU 标识，不能安全修改备注。"
            )
        remark_record = self.get_alarm_remark(worker, alarm_id)
        return {
            "alarmId": alarm_id,
            "rtuId": rtu_id,
            "deviceCode": str(record.get("rtuCode") or "").strip() or None,
            "deviceName": str(record.get("rtuName") or "").strip() or None,
            "alarmType": str(record.get("hitchName") or "").strip() or None,
            "alarmMessage": str(record.get("hitchIntro") or "").strip() or None,
            "alarmState": record.get("conductStatue"),
            "remark": _smartlight_remark_text(remark_record),
            "remarkRecord": deepcopy(remark_record),
        }

    def alarm_action_snapshot(
        self,
        worker,
        alarm_id: str,
        *,
        actionable_view: bool = False,
    ) -> dict:
        record = (
            self._find_actionable_alarm_record(worker, alarm_id)
            if actionable_view
            else self._find_alarm_record(worker, alarm_id)
        )
        state = _smartlight_int(record.get("conductStatue"))
        submit_state = _smartlight_int(record.get("isSubmitWorkArea"))
        if submit_state is None and record.get("isSubmitWorkArea") in (None, ""):
            submit_state = 0
        weight = _smartlight_int(record.get("weightFacto"))
        return {
            "alarmId": alarm_id,
            "rtuId": str(record.get("rtuId") or "").strip() or None,
            "deviceCode": str(record.get("rtuCode") or "").strip() or None,
            "deviceName": str(record.get("rtuName") or "").strip() or None,
            "alarmType": str(record.get("hitchName") or "").strip() or None,
            "alarmMessage": str(record.get("hitchIntro") or "").strip() or None,
            "occurredAt": record.get("occurDate"),
            "lastActivityAt": record.get("lastDate"),
            "alarmState": state,
            "alarmStateLabel": _smartlight_alarm_state_label(state),
            "alarmWeight": weight,
            "workAreaId": str(record.get("workAreaId") or "").strip() or None,
            "workAreaName": str(record.get("workAreaName") or "").strip() or None,
            "workAreaSubmitted": submit_state == 1,
            "workAreaSubmitState": submit_state,
            "workAreaSubmitStateLabel": _smartlight_work_area_state_label(
                submit_state
            ),
        }

    def submit_alarm_to_work_area(self, worker, alarm_id: str) -> int:
        return self._execute_alarm_count_action(
            worker,
            "/rHisHitchAlarm/updateIsSubmitWorkArea",
            {"hitchAlarmIds": alarm_id},
            action_label="提交工区",
        )

    def revoke_alarm_from_work_area(self, worker, alarm_id: str) -> int:
        return self._execute_alarm_count_action(
            worker,
            "/rHisHitchAlarm/cancleSubmitWorkArea",
            {"hitchAlarmIds": alarm_id},
            action_label="撤回工区提交",
        )

    def dispose_rtu_alarm(self, worker, alarm_id: str) -> int:
        return self._execute_alarm_count_action(
            worker,
            "/rHisHitchAlarm/setRtuConductStatusDisposed",
            {"json": alarm_id},
            action_label="处置 RTU 告警",
        )

    def _execute_alarm_count_action(
        self,
        worker,
        path: str,
        fields: dict,
        *,
        action_label: str,
    ) -> int:
        try:
            response = self._authorized_post_response(
                worker,
                path,
                fields,
                retry_after_auth_failure=False,
            )
        except (ConnectionError, TimeoutError) as exc:
            raise SmartlightAlarmActionOutcomeUnknown(
                f"照明系统{action_label}请求的最终结果无法确认。"
            ) from exc
        if response["status"] in {400, 409, 422}:
            raise SmartlightBusinessRuleRejected(
                _smartlight_response_message(response)
            )
        if response["status"] != 200:
            raise SmartlightAlarmActionOutcomeUnknown(
                f"照明系统未能确认{action_label}是否成功"
                f"（HTTP {response['status']}）：{_smartlight_response_message(response)}"
            )
        result = response.get("json")
        try:
            affected = int(result)
        except (TypeError, ValueError) as exc:
            raise SmartlightAlarmActionOutcomeUnknown(
                f"照明系统{action_label}接口没有返回可确认的结果。"
            ) from exc
        if affected < 1:
            raise SmartlightBusinessRuleRejected(
                f"照明系统未执行{action_label}，请重新查看告警状态。"
            )
        return affected

    def get_alarm_remark(self, worker, alarm_id: str) -> dict | None:
        response = self._authorized_post_response(
            worker,
            "/rHisHitchAlarm/getRtuAlarmRemark",
            {"hitchAlarmId": alarm_id},
        )
        if response["status"] != 200:
            raise SmartlightSessionCheckUnavailable(
                f"照明告警备注读取失败（HTTP {response['status']}）。"
            )
        payload = response.get("json")
        if isinstance(payload, dict):
            return payload
        if payload is None and not str(response.get("text") or "").strip():
            return None
        raise SmartlightAlarmRemarkContractMismatch(
            "照明告警备注接口返回了无法识别的数据。"
        )

    def save_alarm_remark(self, worker, payload: dict) -> dict:
        response = self._authorized_post_response(
            worker,
            "/rHisHitchAlarm/saveRtuAlarmRemark",
            {"json": _json_text(payload)},
        )
        if response["status"] in {400, 409, 422}:
            raise SmartlightBusinessRuleRejected(_smartlight_response_message(response))
        if response["status"] != 200:
            raise SmartlightAlarmRemarkOutcomeUnknown(
                "照明系统未能确认告警备注是否保存"
                f"（HTTP {response['status']}）：{_smartlight_response_message(response)}"
            )
        result = response.get("json")
        if not isinstance(result, dict):
            raise SmartlightAlarmRemarkOutcomeUnknown(
                "照明告警备注保存接口没有返回可确认的 JSON 结果。"
            )
        if str(result.get("code") or "") != "200":
            raise SmartlightBusinessRuleRejected(
                _smartlight_response_message(response)
            )
        return result

    def _find_alarm_record(self, worker, alarm_id: str) -> dict:
        context = self._principal_context(worker)
        for page in range(1, 6):
            payload = self._authorized_post_json(
                worker,
                "/rHisHitchAlarm/getDataByRtuAlarm",
                {
                    "json": _json_text(
                        {
                            "_like_params": "",
                            "_include_conductStatue": ["131", "132", "133", "161"],
                            "organroleId": context["organroleId"],
                        }
                    ),
                    "pageNum": page,
                    "pageSize": 100,
                    "organroleId": context["organroleId"],
                },
            )
            page_payload = (
                payload.get("RtuHisHitchAlarm")
                if isinstance(payload, dict)
                else payload
            )
            items = _page_items(page_payload)
            for item in items:
                if str(item.get("hitchAlarmId") or "") == alarm_id:
                    return item
            if not items or page * 100 >= _page_total(page_payload):
                break
        raise SmartlightBusinessRuleRejected(
            "未在当前账号最近 500 条可见 RTU 告警中找到指定告警，已停止修改。"
        )

    def _find_actionable_alarm_record(self, worker, alarm_id: str) -> dict:
        context = self._principal_context(worker)
        filters = {
            "category": None,
            "categoryList": None,
            "usageType": 1,
            "codeOrName": "",
            "_include_conductStatue": ["131", "132"],
            "_include_hitchDicIds": [],
            "_include_isSubmitWorkArea": [],
            "_include_weightFacto": [],
            "conductStatue": [],
            "weightFacto": [1, 2, 3, 4, 5, 6],
            "hitchDicId": "",
            "_timebegin_begin": "",
            "_timeend_end": "",
            "type": "0",
            "_include_groupId": [],
            "groupId": "",
            "dateType": "",
            "showData": True,
            "userId": context["userId"],
            "leakageCurrent": "",
            "reporType": "",
        }
        for page in range(1, 6):
            payload = self._authorized_post_json(
                worker,
                "/rHisHitchAlarm/getDataByRtuAlarm",
                {
                    "json": _json_text(filters),
                    "pageNum": page,
                    "pageSize": 100,
                    "organroleId": context["organroleId"],
                },
            )
            page_payload = (
                payload.get("RtuHisHitchAlarm")
                if isinstance(payload, dict)
                else payload
            )
            items = _page_items(page_payload)
            for item in items:
                if str(item.get("hitchAlarmId") or "") == alarm_id:
                    return item
            if not items or page * 100 >= _page_total(page_payload):
                break
        raise SmartlightBusinessRuleRejected(
            "Target alarm is not present in the Smartlight actionable RTU alarm view."
        )

    def _follow_login_redirects(
        self,
        worker,
        response: dict,
        *,
        timeout_seconds: float,
    ) -> dict:
        for _ in range(5):
            if response["status"] not in {301, 302, 303, 307, 308}:
                return response
            location = response.get("location")
            if not location:
                raise SmartlightLoginContractMismatch(
                    "CAS redirect response has no destination."
                )
            target = _same_origin_url(self.origin, urljoin(response["url"], location))
            response = worker.request_bytes(
                "GET",
                target,
                headers=_CAS_HTML_HEADERS,
                timeout_seconds=timeout_seconds,
            )
        raise SmartlightLoginContractMismatch("CAS login redirect chain is too long.")

    def _cas_principal(self, worker) -> dict:
        response = worker.request(
            "POST",
            self._url("/userInfo/getCasLoginUser"),
            headers=_FORM_HEADERS,
            body="",
            timeout_seconds=20,
        )
        if response["status"] in {301, 302, 303, 307, 308, 401, 403}:
            raise SmartlightLoginRequired("Smartlight CAS session is not authenticated.")
        if response["status"] != 200:
            raise SmartlightSessionCheckUnavailable(
                f"Smartlight principal check failed with HTTP {response['status']}."
            )
        payload = response.get("json")
        if not isinstance(payload, dict):
            if "/cas/login" in str(response.get("url") or "") or "<html" in str(
                response.get("text") or ""
            ).lower():
                raise SmartlightLoginRequired(
                    "Smartlight CAS session is not authenticated."
                )
            raise SmartlightLoginContractMismatch(
                "Smartlight principal response is invalid."
            )
        account = str(payload.get("dlzh") or "").strip()
        name = str(payload.get("userName") or account).strip()
        password_digest = str(payload.get("dlmm") or "").strip()
        organrole_id = str(payload.get("organroleId") or "").strip()
        user_id = str(payload.get("yhid") or "").strip()
        if not any((account, password_digest, organrole_id, user_id)):
            raise SmartlightLoginRequired(
                "Smartlight CAS session is not authenticated."
            )
        if not account or not name or not password_digest or not organrole_id or not user_id:
            raise SmartlightLoginContractMismatch(
                "Smartlight principal response is missing required identity fields."
            )
        return {
            "account": account,
            "name": name,
            "password_digest": password_digest,
            "organId": str(payload.get("organId") or ""),
            "organroleId": organrole_id,
            "organroleName": str(payload.get("organroleName") or ""),
            "personId": str(payload.get("ryid") or ""),
            "userId": user_id,
        }

    def _cas_principal_with_renewal(self, worker) -> tuple[dict, bool]:
        """Recover an expired application session through a still-valid CAS SSO cookie."""
        try:
            return self._cas_principal(worker), False
        except SmartlightLoginRequired as initial_error:
            response = worker.request_bytes(
                "GET",
                self.base_url,
                headers=_CAS_HTML_HEADERS,
                timeout_seconds=20,
            )
            response = self._follow_login_redirects(
                worker,
                response,
                timeout_seconds=20,
            )
            if response["status"] != 200:
                raise SmartlightSessionCheckUnavailable(
                    "Smartlight CAS renewal page is temporarily unavailable."
                )
            if "/cas/login" in str(response.get("url") or ""):
                raise initial_error
            try:
                return self._cas_principal(worker), True
            except SmartlightLoginRequired as renewed_error:
                raise SmartlightLoginRequired(
                    "Smartlight application session could not be renewed through CAS "
                    f"({_session_error_summary(initial_error)}; "
                    f"{_session_error_summary(renewed_error)})."
                ) from renewed_error

    def _exchange_token(self, worker, principal: dict) -> dict:
        response = worker.request(
            "POST",
            f"{self.origin}/jwtcenter//JWTInfoController/getToken",
            headers=_FORM_HEADERS,
            body=urlencode(
                {
                    "subject": principal["account"],
                    "loginPwd": principal["password_digest"],
                }
            ),
            timeout_seconds=20,
        )
        if response["status"] in {301, 302, 303, 307, 308, 401, 403}:
            raise SmartlightLoginRequired("Smartlight CAS session is not authenticated.")
        payload = response.get("json")
        data = payload.get("resp_data") if isinstance(payload, dict) else None
        if response["status"] != 200 or not isinstance(data, dict):
            raise SmartlightSessionCheckUnavailable(
                "Smartlight JWT service is temporarily unavailable."
            )
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token:
            raise SmartlightLoginContractMismatch(
                "Smartlight JWT response has no access token."
            )
        safe_principal = {
            key: value
            for key, value in principal.items()
            if key != "password_digest"
        }
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_duration": data.get("access_token_duration"),
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "principal": safe_principal,
        }

    def _principal_context(self, worker) -> dict:
        state = worker.get_http_state()
        principal = state.get("principal") if isinstance(state, dict) else None
        if not isinstance(principal, dict) or not principal.get("organroleId"):
            self.probe_session(worker)
            state = worker.get_http_state()
            principal = state.get("principal")
        return dict(principal)

    def _authorized_post_json(self, worker, path: str, fields: dict) -> Any:
        response = self._authorized_post_response(worker, path, fields)
        if response["status"] != 200:
            raise SmartlightSessionCheckUnavailable(
                f"Smartlight API failed with HTTP {response['status']}."
            )
        payload = response.get("json")
        if payload is None:
            raise SmartlightSessionCheckUnavailable(
                "Smartlight API did not return JSON."
            )
        return payload

    def _authorized_post_response(
        self,
        worker,
        path: str,
        fields: dict,
        *,
        retry_after_auth_failure: bool = True,
    ) -> dict:
        state = worker.get_http_state()
        token = str(state.get("access_token") or "")
        if not token:
            self.probe_session(worker)
            state = worker.get_http_state()
            token = str(state.get("access_token") or "")
        response = worker.request(
            "POST",
            self._url(path),
            headers={**_FORM_HEADERS, "x-Authentication-Token": token},
            body=urlencode(fields),
            timeout_seconds=30,
        )
        if retry_after_auth_failure and response["status"] in {401, 403}:
            self.probe_session(worker)
            token = str(worker.get_http_state().get("access_token") or "")
            response = worker.request(
                "POST",
                self._url(path),
                headers={**_FORM_HEADERS, "x-Authentication-Token": token},
                body=urlencode(fields),
                timeout_seconds=30,
            )
        if response["status"] in {301, 302, 303, 307, 308, 401, 403}:
            raise SmartlightLoginRequired("Smartlight session is no longer authenticated.")
        return response

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))


def prepare_smartlight_alarm_remark_update(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    inputs = normalize_smartlight_alarm_remark_inputs(arguments)
    snapshot = adapter.alarm_remark_snapshot(worker, inputs["alarm_id"])
    if snapshot["remark"] == inputs["remark"]:
        raise SmartlightBusinessRuleRejected(
            "目标告警的当前备注已经与拟写入内容一致，已停止重复修改。"
        )
    principal = adapter._principal_context(worker)  # noqa: SLF001
    payload = deepcopy(snapshot.get("remarkRecord"))
    if not isinstance(payload, dict):
        payload = {}
    payload.update(
        {
            "hitchAlarmId": snapshot["alarmId"],
            "rtuId": snapshot["rtuId"],
            "remark": inputs["remark"],
        }
    )
    payload.setdefault("createUser", str(principal.get("name") or ""))
    payload.setdefault(
        "createTime",
        datetime.now(_SMARTLIGHT_BUSINESS_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {
        "plan": {
            "schema_version": "agentbridge.smartlight_alarm_remark_update_plan.v1",
            "business_intent": "update_alarm_remark",
            "target": {
                key: snapshot.get(key)
                for key in (
                    "alarmId",
                    "rtuId",
                    "deviceCode",
                    "deviceName",
                    "alarmType",
                    "alarmMessage",
                    "alarmState",
                )
            },
            "exact_input": inputs,
            "exact_payload": payload,
            "preconditions": {
                "previous_remark": snapshot["remark"],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
            "expected_effect": {
                "kind": "update_alarm_remark",
                "alarm_id": snapshot["alarmId"],
                "remark": inputs["remark"],
            },
        },
        "summary": {
            "title": "修改照明 RTU 告警备注",
            "system": SMARTLIGHT_SYSTEM_NAME,
            "effect": "修改一条 RTU 告警备注，可通过再次写入原值恢复",
            "fields": [
                {
                    "label": "告警设备",
                    "value": snapshot.get("deviceName")
                    or snapshot.get("deviceCode")
                    or "未知设备",
                },
                {"label": "告警类型", "value": snapshot.get("alarmType") or "未知"},
                {"label": "告警 ID", "value": snapshot["alarmId"]},
                {
                    "label": "原备注",
                    "value": snapshot["remark"] or "（无）",
                },
                {
                    "label": "新备注",
                    "value": inputs["remark"] or "（清除备注）",
                },
            ],
        },
    }


def commit_smartlight_alarm_remark_update(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary,
) -> dict:
    if plan.get("schema_version") != "agentbridge.smartlight_alarm_remark_update_plan.v1":
        raise SmartlightAlarmRemarkContractMismatch(
            "照明告警备注写入计划版本不受支持。"
        )
    inputs = deepcopy(plan.get("exact_input"))
    payload = deepcopy(plan.get("exact_payload"))
    target = deepcopy(plan.get("target"))
    preconditions = deepcopy(plan.get("preconditions"))
    if not all(isinstance(value, dict) for value in (inputs, payload, target, preconditions)):
        raise SmartlightAlarmRemarkContractMismatch(
            "照明告警备注写入计划缺少冻结字段。"
        )
    current = adapter.alarm_remark_snapshot(worker, inputs["alarm_id"])
    if str(current.get("rtuId") or "") != str(target.get("rtuId") or ""):
        raise SmartlightBusinessRuleRejected(
            "授权后目标告警关联的 RTU 已变化，已停止写入。"
        )
    if current["remark"] != str(preconditions.get("previous_remark") or ""):
        raise SmartlightBusinessRuleRejected(
            "授权后目标告警备注已被其他操作修改，已停止覆盖。"
        )
    enter_commit_boundary()
    submission_returned = False
    try:
        downstream = adapter.save_alarm_remark(worker, payload)
        submission_returned = True
        readback = adapter.alarm_remark_snapshot(worker, inputs["alarm_id"])
        if readback["remark"] != inputs["remark"]:
            raise SmartlightAlarmRemarkOutcomeUnknown(
                "照明系统接受了备注保存请求，但权威回读未得到预期内容。"
            )
    except SmartlightBusinessRuleRejected as exc:
        if submission_returned:
            raise SmartlightAlarmRemarkOutcomeUnknown("照明备注提交后回读被拒绝，无法确认结果；请先对账。") from exc
        raise
    except SmartlightAlarmRemarkOutcomeUnknown:
        raise
    except Exception as exc:
        raise SmartlightAlarmRemarkOutcomeUnknown(
            f"照明告警备注已进入提交边界，但提交应答或权威回读异常（{type(exc).__name__}）；请先对账。"
        ) from exc
    previous_remark = str(preconditions.get("previous_remark") or "")
    return {
        "status": "updated",
        "effect": (
            "告警备注已清除，并经照明系统权威接口回读确认。"
            if not inputs["remark"]
            else "告警备注已更新，并经照明系统权威接口回读确认。"
        ),
        "alarm": {
            key: readback.get(key)
            for key in (
                "alarmId",
                "deviceCode",
                "deviceName",
                "alarmType",
                "alarmMessage",
                "alarmState",
                "remark",
            )
        },
        "verification": {
            "method": "POST /rHisHitchAlarm/getRtuAlarmRemark",
            "matched": True,
        },
        "rollback": {
            "available": True,
            "capability": SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY,
            "arguments": {
                "alarm_id": inputs["alarm_id"],
                "remark": previous_remark,
            },
        },
        "downstream": {"code": downstream.get("code")},
    }


def prepare_smartlight_alarm_work_area_submit(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    return _prepare_smartlight_alarm_action(
        adapter,
        worker,
        arguments,
        action="submit_work_area",
    )


def prepare_smartlight_alarm_work_area_revoke(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    return _prepare_smartlight_alarm_action(
        adapter,
        worker,
        arguments,
        action="revoke_work_area",
    )


def prepare_smartlight_rtu_alarm_dispose(
    adapter,
    worker,
    arguments: dict,
) -> dict:
    return _prepare_smartlight_alarm_action(
        adapter,
        worker,
        arguments,
        action="dispose",
    )


def _prepare_smartlight_alarm_action(
    adapter,
    worker,
    arguments: dict,
    *,
    action: str,
) -> dict:
    alarm_id = _normalize_smartlight_alarm_id(arguments)
    snapshot = adapter.alarm_action_snapshot(
        worker,
        alarm_id,
        actionable_view=action in {"submit_work_area", "revoke_work_area"},
    )
    if not snapshot.get("rtuId"):
        raise SmartlightAlarmActionContractMismatch(
            "目标告警缺少 RTU 标识，不能安全执行写操作。"
        )
    state = snapshot.get("alarmState")
    submit_state = snapshot.get("workAreaSubmitState")
    if action in {"submit_work_area", "revoke_work_area"} and submit_state not in {
        0,
        1,
    }:
        raise SmartlightBusinessRuleRejected(
            "该告警的工区提交状态无法识别，已停止执行。"
        )
    if action == "submit_work_area":
        if state not in {0, 1}:
            raise SmartlightBusinessRuleRejected(
                "该告警已经解除或处置，不能提交工区。"
            )
        if snapshot.get("alarmWeight") != 3:
            raise SmartlightBusinessRuleRejected(
                "该告警等级不是工区接收范围，照明系统不允许提交。"
            )
        if not snapshot.get("workAreaId") or not snapshot.get("workAreaName"):
            raise SmartlightBusinessRuleRejected(
                "该告警没有有效所属工区，不能提交工区。"
            )
        if snapshot.get("workAreaSubmitted"):
            raise SmartlightBusinessRuleRejected(
                "该告警已经提交到工区，本次没有重复写入。"
            )
        schema_version = "agentbridge.smartlight_alarm_work_area_submit_plan.v1"
        title = "提交照明 RTU 告警到工区"
        effect = "把一条 RTU 告警提交给所属工区处理"
        expected_effect = {"work_area_submitted": True}
        risk_notice = "提交成功后可另行发起撤回授权，但不会后台自动撤回。"
    elif action == "revoke_work_area":
        if not snapshot.get("workAreaSubmitted"):
            raise SmartlightBusinessRuleRejected(
                "该告警当前未提交工区，无需撤回。"
            )
        schema_version = "agentbridge.smartlight_alarm_work_area_revoke_plan.v1"
        title = "撤回照明 RTU 告警的工区提交"
        effect = "撤回一条已经提交给工区处理的 RTU 告警"
        expected_effect = {"work_area_submitted": False}
        risk_notice = "撤回后如需重新提交，必须重新生成并确认授权卡。"
    elif action == "dispose":
        if state == 2:
            raise SmartlightBusinessRuleRejected(
                "该告警已经解除，不能再标记为已处置。"
            )
        if state == 3:
            raise SmartlightBusinessRuleRejected(
                "该告警已经处置，本次没有重复写入。"
            )
        if state not in {0, 1}:
            raise SmartlightBusinessRuleRejected(
                "该告警当前状态不允许处置。"
            )
        schema_version = "agentbridge.smartlight_rtu_alarm_dispose_plan.v1"
        title = "处置照明 RTU 告警"
        effect = "把一条 RTU 告警永久标记为已处置"
        expected_effect = {"alarm_state": 3}
        risk_notice = "目标系统未发现撤销处置接口；确认后不能由 AgentBridge 恢复。"
    else:
        raise SmartlightAlarmActionContractMismatch("不支持的 RTU 告警动作。")

    target = deepcopy(snapshot)
    return {
        "plan": {
            "schema_version": schema_version,
            "business_intent": action,
            "target": target,
            "exact_input": {"alarm_id": alarm_id},
            "preconditions": _smartlight_alarm_preconditions(target),
            "expected_effect": expected_effect,
        },
        "summary": {
            "title": title,
            "system": SMARTLIGHT_SYSTEM_NAME,
            "effect": effect,
            "authorization_notice": risk_notice,
            "authorize_label": "确认并执行",
            "fields": _smartlight_alarm_summary_fields(target),
        },
    }


def commit_smartlight_alarm_work_area_submit(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary,
) -> dict:
    return _commit_smartlight_alarm_action(
        adapter,
        worker,
        plan,
        action="submit_work_area",
        schema_version="agentbridge.smartlight_alarm_work_area_submit_plan.v1",
        execute=adapter.submit_alarm_to_work_area,
        enter_commit_boundary=enter_commit_boundary,
    )


def commit_smartlight_alarm_work_area_revoke(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary,
) -> dict:
    return _commit_smartlight_alarm_action(
        adapter,
        worker,
        plan,
        action="revoke_work_area",
        schema_version="agentbridge.smartlight_alarm_work_area_revoke_plan.v1",
        execute=adapter.revoke_alarm_from_work_area,
        enter_commit_boundary=enter_commit_boundary,
    )


def commit_smartlight_rtu_alarm_dispose(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary,
) -> dict:
    return _commit_smartlight_alarm_action(
        adapter,
        worker,
        plan,
        action="dispose",
        schema_version="agentbridge.smartlight_rtu_alarm_dispose_plan.v1",
        execute=adapter.dispose_rtu_alarm,
        enter_commit_boundary=enter_commit_boundary,
    )


def _commit_smartlight_alarm_action(
    adapter,
    worker,
    plan: dict,
    *,
    action: str,
    schema_version: str,
    execute,
    enter_commit_boundary,
) -> dict:
    if plan.get("schema_version") != schema_version:
        raise SmartlightAlarmActionContractMismatch(
            "照明 RTU 告警写入计划版本不受支持。"
        )
    inputs = plan.get("exact_input")
    target = plan.get("target")
    preconditions = plan.get("preconditions")
    if not all(isinstance(value, dict) for value in (inputs, target, preconditions)):
        raise SmartlightAlarmActionContractMismatch(
            "照明 RTU 告警写入计划缺少冻结字段。"
        )
    alarm_id = _normalize_smartlight_alarm_id(inputs)
    actionable_view = action in {"submit_work_area", "revoke_work_area"}
    current = adapter.alarm_action_snapshot(
        worker,
        alarm_id,
        actionable_view=actionable_view,
    )
    if str(current.get("rtuId") or "") != str(target.get("rtuId") or ""):
        raise SmartlightBusinessRuleRejected(
            "授权后目标告警关联的 RTU 已变化，已停止执行，请重新查看。"
        )
    if _smartlight_alarm_preconditions(current) != preconditions:
        if _smartlight_alarm_action_reached(current, action):
            enter_commit_boundary()
            return _smartlight_alarm_action_result(
                current,
                action=action,
                status="already_completed",
                affected=0,
            )
        raise SmartlightBusinessRuleRejected(
            "授权后告警状态、所属工区或提交状态已经变化，已停止执行，请重新查看。"
        )
    enter_commit_boundary()
    affected = execute(worker, alarm_id)
    try:
        readback = adapter.alarm_action_snapshot(
            worker,
            alarm_id,
            actionable_view=actionable_view,
        )
    except Exception as exc:
        raise SmartlightAlarmActionOutcomeUnknown(
            "照明系统接受了写入请求，但权威回读失败，最终结果无法确认。"
        ) from exc
    if not _smartlight_alarm_action_reached(readback, action):
        raise SmartlightAlarmActionOutcomeUnknown(
            "照明系统接受了写入请求，但权威回读未得到预期状态。"
        )
    return _smartlight_alarm_action_result(
        readback,
        action=action,
        status="succeeded",
        affected=affected,
    )


def _smartlight_alarm_action_result(
    snapshot: dict,
    *,
    action: str,
    status: str,
    affected: int,
) -> dict:
    rollback = {"available": False}
    if action == "submit_work_area":
        rollback = {
            "available": True,
            "capability": SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
            "arguments": {"alarm_id": snapshot["alarmId"]},
        }
    elif action == "revoke_work_area":
        rollback = {
            "available": True,
            "capability": SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
            "arguments": {"alarm_id": snapshot["alarmId"]},
        }
    return {
        "status": status,
        "action": action,
        "alarm": snapshot,
        "verification": {
            "method": "POST /rHisHitchAlarm/getDataByRtuAlarm",
            "matched": True,
        },
        "rollback": rollback,
        "downstream": {"affected": affected},
    }


def _smartlight_alarm_action_reached(snapshot: dict, action: str) -> bool:
    if action == "submit_work_area":
        return snapshot.get("workAreaSubmitState") == 1
    if action == "revoke_work_area":
        return snapshot.get("workAreaSubmitState") == 0
    if action == "dispose":
        return snapshot.get("alarmState") == 3
    return False


def _smartlight_alarm_preconditions(snapshot: dict) -> dict:
    return {
        key: snapshot.get(key)
        for key in (
            "alarmId",
            "rtuId",
            "alarmState",
            "alarmWeight",
            "workAreaId",
            "workAreaName",
            "workAreaSubmitState",
        )
    }


def _smartlight_alarm_summary_fields(snapshot: dict) -> list[dict]:
    return [
        {
            "label": "RTU",
            "value": snapshot.get("deviceName")
            or snapshot.get("deviceCode")
            or "未知 RTU",
        },
        {"label": "RTU 编号", "value": snapshot.get("deviceCode") or "未知"},
        {"label": "告警类型", "value": snapshot.get("alarmType") or "未知"},
        {"label": "告警内容", "value": snapshot.get("alarmMessage") or "未知"},
        {"label": "告警 ID", "value": snapshot["alarmId"]},
        {"label": "当前状态", "value": snapshot.get("alarmStateLabel") or "未知"},
        {
            "label": "所属工区",
            "value": snapshot.get("workAreaName")
            or snapshot.get("workAreaId")
            or "未配置",
        },
        {
            "label": "工区提交状态",
            "value": snapshot.get("workAreaSubmitStateLabel") or "未知",
        },
        {"label": "首次发生", "value": snapshot.get("occurredAt") or "未知"},
        {"label": "最近发生", "value": snapshot.get("lastActivityAt") or "未知"},
    ]


def normalize_smartlight_alarm_remark_inputs(arguments: dict) -> dict:
    alarm_id = str(arguments.get("alarm_id") or "").strip()
    if not alarm_id or len(alarm_id) > 200:
        raise SmartlightAlarmRemarkContractMismatch(
            "alarm_id 不能为空且不能超过 200 个字符。"
        )
    remark = str(arguments.get("remark") or "").strip()
    if len(remark) > 500:
        raise SmartlightAlarmRemarkContractMismatch(
            "告警备注不能超过 500 个字符。"
        )
    return {"alarm_id": alarm_id, "remark": remark}


def _normalize_smartlight_alarm_id(arguments: dict) -> str:
    alarm_id = str(arguments.get("alarm_id") or "").strip()
    if not alarm_id or len(alarm_id) > 200:
        raise SmartlightAlarmActionContractMismatch(
            "alarm_id 不能为空且不能超过 200 个字符。"
        )
    return alarm_id


def build_smartlight_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    specs = (
        CapabilitySpec(
            name=SMARTLIGHT_OVERVIEW_CAPABILITY,
            version="0.2.0",
            description=(
                "Summarize visible Smartlight cabinets and report both searchable "
                "and map-detail lamp-post counts."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-system-overview-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LAMPPOST_LIST_CAPABILITY,
            version="0.1.0",
            description="List lamp posts visible to the authenticated Smartlight user.",
            input_schema=_paged_schema({"keyword": {"type": "string"}}),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-lamppost-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_RUNTIME_OVERVIEW_CAPABILITY,
            version="0.1.0",
            description=(
                "Read the current RTU and single-lamp runtime snapshots without "
                "mixing them with the registered asset inventory."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-runtime-overview-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY,
            version="0.1.0",
            description="List RTUs from the lighting runtime page with explicit state scope.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["all", "online", "offline", "power_off", "disabled"],
                    },
                    "alarm_only": {"type": "boolean"},
                    "work_area": {"type": "string"},
                    "group": {"type": "string"},
                    "model": {"type": "string"},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-rtu-status-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY,
            version="0.1.0",
            description="List single-lamp controllers and their observed electrical state.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "controller_state": {
                        "type": "string",
                        "enum": ["all", "online", "offline"],
                    },
                    "lamp_state": {
                        "type": "string",
                        "enum": ["all", "on", "off", "abnormal"],
                    },
                    "alarm_only": {"type": "boolean"},
                    "street": {"type": "string"},
                    "cabinet": {"type": "string"},
                    "work_area": {"type": "string"},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-lamp-status-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY,
            version="0.1.0",
            description="List bounded single-lamp alarms with their original alarm semantics.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "alarm_type": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "non_current"],
                    },
                    "road": {"type": "string"},
                    "work_area": {"type": "string"},
                    "cabinet": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 3660},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-lamp-alarm-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LAMP_ALARM_ANALYSIS_CAPABILITY,
            version="0.1.0",
            description="Analyze at most 500 single-lamp alarms by day, type, lamp post and road.",
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "alarm_type": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "non_current"],
                    },
                    "road": {"type": "string"},
                    "work_area": {"type": "string"},
                    "cabinet": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 3660},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-lamp-alarm-analysis-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_RTU_SURVEY_RECORDS_CAPABILITY,
            version="0.1.0",
            description="Read a bounded RTU survey history window of at most seven days.",
            input_schema=_paged_schema(
                {
                    "rtu_id": {"type": "string"},
                    "rtu_keyword": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "abnormal_only": {"type": "boolean"},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-rtu-survey-records-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY,
            version="0.1.0",
            description="Read bounded daily RTU energy values from the energy-record page.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "device_id": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 92},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-energy-record-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ENERGY_ANALYSIS_CAPABILITY,
            version="0.1.0",
            description="Analyze bounded downstream daily energy values without inventing units.",
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "device_id": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 366},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-energy-analysis-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY,
            version="0.1.0",
            description="Read bounded single-lamp telemetry history, not inspection-task records.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "lamp_post_id": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-lamp-survey-records-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_RTU_LEAKAGE_ALARM_LIST_CAPABILITY,
            version="0.1.0",
            description="List real RTU branch-leakage alarms from the dedicated leakage page.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "cleared"],
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 366},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-rtu-leakage-alarm-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_RTU_LEAKAGE_ANALYSIS_CAPABILITY,
            version="0.1.0",
            description="Analyze at most 500 dedicated RTU branch-leakage alarm records.",
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "cleared"],
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 366},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-rtu-leakage-analysis-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_OFF_HOURS_CURRENT_LIST_CAPABILITY,
            version="0.1.0",
            description="Read current observations recorded by the off-hours-current page.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-off-hours-current-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_INSPECTION_LOG_LIST_CAPABILITY,
            version="0.1.0",
            description="Read inspection-log group statistics without inferring person-level events.",
            input_schema=_paged_schema(
                {
                    "plan_id": {"type": "string"},
                    "group": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 366},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-inspection-log-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_MAINTENANCE_RECORD_LIST_CAPABILITY,
            version="0.1.0",
            description="List bounded RTU and lamp-post maintenance records.",
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "overhaul_user": {"type": "string"},
                    "device_type": {
                        "type": "string",
                        "enum": ["all", "rtu", "lamppost"],
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 366},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-maintenance-record-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ALARM_LIST_CAPABILITY,
            version="0.3.0",
            description=(
                "List RTU alarms visible to the authenticated Smartlight user, "
                "globally ordered by first occurrence or latest activity before paging."
            ),
            input_schema=_paged_schema(
                {
                    "keyword": {"type": "string"},
                    "sort_by": {
                        "type": "string",
                        "enum": ["occurred_at", "last_activity"],
                    },
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-alarm-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ALARM_REMARK_GET_CAPABILITY,
            version="0.1.0",
            description="Read the authoritative current remark for one RTU alarm.",
            input_schema={
                "type": "object",
                "properties": {"alarm_id": {"type": "string"}},
                "required": ["alarm_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-alarm-remark-get-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY,
            version="0.2.1",
            description=(
                "List Smartlight inspection tasks with task, plan and state filters, "
                "including group, schedule, progress and device counts."
            ),
            input_schema=_paged_schema(
                {
                    "task_name": {"type": "string"},
                    "plan_name": {"type": "string"},
                    "state": {"type": ["integer", "string", "null"]},
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-inspection-task-list-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LEAKAGE_SUMMARY_CAPABILITY,
            version="0.3.0",
            description=(
                "Deprecated compatibility alias for the single-lamp alarm list; "
                "the underlying records are not a leakage-only dataset."
            ),
            input_schema=_paged_schema(
                {
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3660,
                    },
                }
            ),
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-leakage-summary-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ASSET_SEARCH_CAPABILITY,
            version="0.1.0",
            description=(
                "Search visible Smartlight control cabinets, RTUs, or lamp posts "
                "through one normalized asset contract."
            ),
            input_schema={
                **_paged_schema(
                    {
                    "asset_type": {
                        "type": "string",
                        "enum": ["cabinet", "rtu", "lamppost"],
                    },
                    "keyword": {"type": "string"},
                    }
                ),
                "required": ["asset_type"],
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-asset-search-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ASSET_DETAIL_CAPABILITY,
            version="0.1.0",
            description=(
                "Read an exact Smartlight asset by ID; RTU details include relay "
                "and circuit structure."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "enum": ["cabinet", "rtu", "lamppost"],
                    },
                    "asset_id": {"type": "string"},
                },
                "required": ["asset_type", "asset_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-asset-detail-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_ALARM_ANALYSIS_CAPABILITY,
            version="0.1.0",
            description=(
                "Analyze a bounded date range of Smartlight RTU alarms by state, "
                "type, device, and day."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "alarm_type": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "cleared"],
                    },
                    "time_field": {
                        "type": "string",
                        "enum": ["last_activity", "occurred"],
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 3660},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-alarm-analysis-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_INSPECTION_TASK_DETAIL_CAPABILITY,
            version="0.1.0",
            description=(
                "Read daily progress and observed clock-in records for an exact "
                "Smartlight inspection task."
            ),
            input_schema={
                **_paged_schema(
                    {
                    "task_id": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "detail_date": {"type": "string"},
                    "clockin_user": {"type": "string"},
                    "has_issues": {"type": ["boolean", "null"]},
                    }
                ),
                "required": ["task_id"],
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-inspection-task-detail-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_LEAKAGE_ANALYSIS_CAPABILITY,
            version="0.2.0",
            description=(
                "Deprecated compatibility alias for single-lamp alarm analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 3660},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-leakage-analysis-v1",
        ),
        CapabilitySpec(
            name=SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            version="0.2.0",
            description=(
                "Export a bounded Smartlight alarm, leakage, asset, or inspection "
                "report through the generic AgentBridge file-delivery contract."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": [
                            "alarm_analysis",
                            "lamp_alarm_analysis",
                            "leakage_analysis",
                            "asset_inventory",
                            "inspection_progress",
                            "energy_records",
                            "energy_analysis",
                            "lamp_survey_records",
                            "rtu_leakage_alarms",
                            "rtu_leakage_analysis",
                            "inspection_logs",
                            "maintenance_records",
                        ],
                    },
                    "asset_type": {
                        "type": "string",
                        "enum": ["cabinet", "rtu", "lamppost"],
                    },
                    "keyword": {"type": "string"},
                    "alarm_type": {"type": "string"},
                    "alarm_state": {
                        "type": "string",
                        "enum": ["all", "current", "cleared"],
                    },
                    "time_field": {
                        "type": "string",
                        "enum": ["last_activity", "occurred"],
                    },
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "last_days": {"type": "integer", "minimum": 1, "maximum": 3660},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                    "task_id": {"type": "string"},
                    "detail_date": {"type": "string"},
                    "clockin_user": {"type": "string"},
                    "has_issues": {"type": ["boolean", "null"]},
                    "device_id": {"type": "string"},
                    "lamp_post_id": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "group": {"type": "string"},
                    "overhaul_user": {"type": "string"},
                    "device_type": {
                        "type": "string",
                        "enum": ["all", "rtu", "lamppost"],
                    },
                },
                "required": ["report_type"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effect="read",
            adapter=SMARTLIGHT_ADAPTER_ID,
            workflow="smartlight-report-export-v1",
        ),
        *SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW.capability_specs(),
        *SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_WORKFLOW.capability_specs(),
        *SMARTLIGHT_ALARM_WORK_AREA_REVOKE_WORKFLOW.capability_specs(),
        *SMARTLIGHT_RTU_ALARM_DISPOSE_WORKFLOW.capability_specs(),
    )
    for spec in specs:
        registry.register(spec)
    return registry


class _CasLoginParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.form_action: str | None = None
        self.captcha_src: str | None = None
        self.hidden_fields: dict[str, str] = {}
        self.error_message = ""
        self._in_auth_form = False
        self._in_error = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form" and values.get("id") == "auth-form":
            self._in_auth_form = True
            self.form_action = values.get("action") or None
            return
        if tag == "div" and "login-error-info" in values.get("class", "").split():
            self._in_error = True
            return
        if tag == "img" and values.get("id") == "captchaImage":
            self.captcha_src = values.get("src") or None
            return
        if (
            tag == "input"
            and self._in_auth_form
            and values.get("type", "").lower() == "hidden"
            and values.get("name") in {"lt", "execution", "_eventId"}
        ):
            self.hidden_fields[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_error:
            self._in_error = False
        if tag == "form":
            self._in_auth_form = False

    def handle_data(self, data: str) -> None:
        if self._in_error:
            text = " ".join(data.split())
            if text:
                self.error_message = " ".join(
                    part for part in (self.error_message, text) if part
                )


def _login_rejection_code(message: str) -> str:
    normalized = "".join(str(message or "").split())
    if "验证码" in normalized:
        return "CAPTCHA_REJECTED"
    if any(token in normalized for token in ("用户名", "账号", "密码", "凭证")):
        return "CREDENTIALS_REJECTED"
    return "AUTHENTICATION_REJECTED"


def _smartlight_remark_text(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("remark") or "").strip()


def _normalize_smartlight_alarm_read_id(arguments: dict) -> str:
    alarm_id = str(arguments.get("alarm_id") or "").strip()
    if not alarm_id or len(alarm_id) > 200:
        raise ValueError("alarm_id 不能为空且不能超过 200 个字符。")
    return alarm_id


def _smartlight_alarm_order_verified(items: list[dict], sort_field: str) -> bool:
    previous: str | None = None
    missing_seen = False
    for item in items:
        value = str(item.get(sort_field) or "").strip()
        if not value:
            missing_seen = True
            continue
        if missing_seen or (previous is not None and previous < value):
            return False
        previous = value
    return True


def _smartlight_stabilize_alarm_order(
    items: list[dict],
    sort_field: str,
) -> list[dict]:
    secondary_field = (
        "lastActivityAt" if sort_field == "occurredAt" else "occurredAt"
    )
    ordered = list(items)
    ordered.sort(key=lambda item: str(item.get("id") or ""))
    ordered.sort(
        key=lambda item: str(item.get(secondary_field) or ""),
        reverse=True,
    )
    ordered.sort(
        key=lambda item: str(item.get(sort_field) or ""),
        reverse=True,
    )
    return ordered


def _smartlight_latest_alarm_group(
    items: list[dict],
    *,
    total: int,
    sort_field: str,
) -> dict:
    if not items:
        return {
            "timestamp": None,
            "sortField": sort_field,
            "observedCount": 0,
            "exactCount": 0,
            "complete": True,
            "candidates": [],
        }
    timestamp = str(items[0].get(sort_field) or "").strip() or None
    tied: list[dict] = []
    for item in items:
        if (str(item.get(sort_field) or "").strip() or None) != timestamp:
            break
        tied.append(item)
    complete = len(tied) < len(items) or len(items) >= total
    return {
        "timestamp": timestamp,
        "sortField": sort_field,
        "observedCount": len(tied),
        "exactCount": len(tied) if complete else None,
        "complete": complete,
        "candidates": [
            {
                "id": item.get("id"),
                "device": item.get("device"),
                "deviceCode": item.get("deviceCode"),
                "type": item.get("type"),
                "message": item.get("message"),
                "occurredAt": item.get("occurredAt"),
                "lastActivityAt": item.get("lastActivityAt"),
            }
            for item in tied
        ],
        "selectionPolicy": (
            "Read-only requests may select one candidate deterministically; "
            "write requests must disambiguate tied alarms by business fields."
        ),
    }


def _smartlight_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _smartlight_alarm_state_label(value: int | None) -> str:
    return {
        0: "当前告警",
        1: "非当前告警",
        2: "已解除报警",
        3: "已处置",
    }.get(value, "未知状态")


def _smartlight_work_area_state_label(value: int | None) -> str:
    return {0: "未提交", 1: "已提交"}.get(value, "未知状态")


def _smartlight_response_message(response: dict) -> str:
    payload = response.get("json")
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "error_description"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:500]
    text = str(response.get("text") or "").strip()
    if text:
        return text[:500]
    return "照明系统未返回具体原因。"


def _session_error_summary(error: Exception) -> str:
    message = " ".join(str(error).split())
    if len(message) > 300:
        message = f"{message[:297]}..."
    return f"{error.__class__.__name__}: {message or 'no details'}"


def _jwt_is_expired(token: str, *, now: datetime | None = None) -> bool:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return False
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        expires_at = float(payload["exp"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    checked_at = now or datetime.now(timezone.utc)
    return expires_at <= checked_at.timestamp()


def _refresh_response_requires_login(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("resp_code") or "").strip()
    if code in _SMARTLIGHT_REFRESH_LOGIN_REQUIRED_CODES:
        return True
    message = " ".join(str(payload.get("resp_msg") or "").lower().split())
    return "refresh_token" in message and any(
        marker in message
        for marker in ("失效", "过期", "重新登录", "expired", "invalid")
    )


def _same_origin_url(origin: str, value: Any) -> str:
    target = urljoin(f"{origin}/", str(value or ""))
    parsed = urlparse(target)
    target_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if target_origin != origin:
        raise SmartlightLoginContractMismatch("CAS URL escaped the registered origin.")
    return target


def _paged_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            **properties,
            "page": {"type": "integer"},
            "size": {"type": "integer"},
        },
        "additionalProperties": False,
    }


def _single_filter_list(value: Any) -> list[str]:
    normalized = str(value or "").strip()
    return [normalized] if normalized else []


def _contains_text(value: Any, expected: str) -> bool:
    return not expected or expected.casefold() in str(value or "").casefold()


def _rtu_status_query(
    *,
    keyword: str = "",
    work_area: str = "",
    group: str = "",
    model: str = "",
) -> dict:
    return {
        "_like_params": keyword,
        "_include_transTypeId": [],
        "_include_productModelId": _single_filter_list(model),
        "_include_rtuTypeId": [],
        "_include_groupId": _single_filter_list(group),
        "_include_workAreaId": _single_filter_list(work_area),
        "_include_workModeId": [],
        "_include_streetId": [],
        "duration": None,
        "WorkModel": None,
        "filterParam": None,
        "filterParamFromIndex": None,
    }


def _rtu_state_filter(state: str) -> str | None:
    return {
        "all": None,
        "offline": "NoOnlineWithNoHandle",
        "power_off": "PowerOutage",
        "disabled": "Disable",
    }.get(state)


def _lamp_status_filter(controller_state: str, lamp_state: str) -> str | None:
    if controller_state == "online" and lamp_state == "all":
        return "ONLINE"
    if controller_state == "offline" and lamp_state == "all":
        return "OFFLINE"
    if controller_state == "all" and lamp_state == "on":
        return "OPEN"
    if controller_state == "all" and lamp_state == "off":
        return "CLOSE"
    return None


def _smartlight_bool(value: Any) -> bool | None:
    if value in (True, 1, "1", "true", "True", "yes", "YES"):
        return True
    if value in (False, 0, "0", "false", "False", "no", "NO"):
        return False
    return None


def _normalize_rtu_status(item: dict, *, requested_state: str) -> dict:
    state_code = _smartlight_int(_first(item, "rtuRunningState", "runningState"))
    state_label = {
        "online": "在线",
        "offline": "离线",
        "power_off": "电源停电",
        "disabled": "未启用",
    }.get(requested_state)
    if state_label is None:
        state_label = {
            1: "在线",
            2: "在线",
            4: "离线或电源停电",
            5: "未启用",
        }.get(state_code, "未知")
    phase = item.get("coplogPhase") if isinstance(item.get("coplogPhase"), dict) else {}
    alarm_list = item.get("rtuAlarmList")
    has_alarm = _smartlight_bool(item.get("isAlarm"))
    if has_alarm is None and isinstance(alarm_list, list):
        has_alarm = bool(alarm_list)
    return {
        "id": _first(item, "rtuId", "id"),
        "code": _first(item, "rtuCode", "code"),
        "name": _first(item, "rtuName", "name"),
        "modelId": _first(item, "rtuProductModelId", "productModelId"),
        "model": _first(item, "rtuProductModelName", "productModelName", "productModel"),
        "cabinetId": _first(item, "controlCabinetId"),
        "cabinet": _first(item, "controlCabinetName"),
        "workAreaId": _first(item, "workAreaId"),
        "workArea": _first(item, "workAreaName"),
        "groupId": _first(item, "rtuGroupId", "groupId"),
        "group": _first(item, "rtuGroupName", "groupName"),
        "road": _first(item, "streetName"),
        "side": _first(item, "streetSideName"),
        "state": state_label,
        "stateCode": state_code,
        "stateScope": (
            "selected_downstream_filter" if requested_state != "all" else "record_state_code"
        ),
        "lastOnlineAt": _first(item, "lastOnlineTime"),
        "lastSurveyAt": _first(item, "coplogTime", "lastCoplogTime", "addDate"),
        "hasAlarm": has_alarm,
        "alarms": _bounded_json(alarm_list or []),
        "isOpen": _smartlight_bool(item.get("isOpen")),
        "isEnabled": _smartlight_bool(item.get("isEnabled")),
        "isSucceeded": _smartlight_bool(item.get("isSucceeded")),
        "workModes": _bounded_json(_first(item, "workModels", "workModeName")),
        "relayNumbers": _bounded_json(item.get("relayNums")),
        "telemetry": {
            "phaseVoltage": {
                "a": _first(phase, "strRtuScaleU1", "Ua", "ua"),
                "b": _first(phase, "strRtuScaleU2", "Ub", "ub"),
                "c": _first(phase, "strRtuScaleU3", "Uc", "uc"),
            },
            "phaseCurrent": {
                "a": _first(phase, "strRtuScaleIsp1", "Ia", "ia"),
                "b": _first(phase, "strRtuScaleIsp2", "Ib", "ib"),
                "c": _first(phase, "strRtuScaleIsp3", "Ic", "ic"),
            },
            "temperature": _first(phase, "temperature"),
            "humidity": _first(phase, "humidity"),
            "leakCurrents": _bounded_json(
                _first(phase, "LeakCurrents", "relayLeakCurrents")
            ),
        },
    }


def _normalize_lamp_status(item: dict) -> dict:
    raw_lamps = item.get("AloneLamps") if isinstance(item.get("AloneLamps"), list) else []
    lamps = [_normalize_single_lamp(lamp) for lamp in raw_lamps if isinstance(lamp, dict)]
    online_values = [lamp["controllerOnline"] for lamp in lamps if lamp["controllerOnline"] is not None]
    controller_online = any(online_values) if online_values else None
    has_alarm = any(lamp["hasAlarm"] for lamp in lamps)
    last_activity = max(
        (str(lamp.get("lastActivityAt") or "") for lamp in lamps),
        default="",
    ) or None
    if any(lamp["switchOn"] is True for lamp in lamps):
        lamp_state = "on"
    elif any(lamp["switchOn"] is False for lamp in lamps):
        lamp_state = "off"
    else:
        lamp_state = "unknown"
    return {
        "id": _first(item, "LampPostID", "lampPostId", "id"),
        "code": _first(item, "LampPostCode", "lampPostCode"),
        "type": _first(item, "LampPostType", "lampPostTypeName"),
        "road": _first(item, "StreetName", "streetName"),
        "side": _first(item, "StreetSideName", "streetSideName"),
        "workArea": _first(item, "WorkAreaName", "workAreaName"),
        "cabinetCode": _first(item, "controlCabinetCode"),
        "cabinet": _first(item, "controlCabinetName"),
        "rtuCode": _first(item, "rtuCode"),
        "rtuName": _first(item, "rtuName"),
        "controllerOnline": controller_online,
        "controllerState": (
            "online" if controller_online is True else "offline" if controller_online is False else "unknown"
        ),
        "lastActivityAt": last_activity,
        "lampState": "abnormal" if has_alarm else lamp_state,
        "hasAlarm": has_alarm,
        "lampCount": len(lamps),
        "lamps": lamps,
    }


def _normalize_single_lamp(item: dict) -> dict:
    alarms = item.get("hitchAlarms") if isinstance(item.get("hitchAlarms"), list) else []
    return {
        "id": _first(item, "AloneLampId", "aloneLampId", "id"),
        "controllerId": _first(item, "aloneLampControlId"),
        "number": _first(item, "LampNumber", "lampNumber"),
        "code": _first(item, "LampCode", "lampCode"),
        "effect": _first(item, "Effect", "lampEffectName"),
        "controllerOnline": _smartlight_bool(_first(item, "IsOnline", "online")),
        "switchOn": _smartlight_bool(_first(item, "IsSwitchOn", "switchOn")),
        "lastActivityAt": _first(item, "copDate", "deviceTime"),
        "voltage": _first(item, "U", "voltage"),
        "current": _first(item, "I", "lampIsp"),
        "powerFactor": _first(item, "Pf", "powerFactor"),
        "activePower": _first(item, "Ap", "activePower"),
        "dimming": _first(item, "dimmingValue"),
        "energy": _first(item, "energy"),
        "leakageVoltage": _first(item, "leakageVoltage"),
        "leakageCurrent": _first(item, "leakageCurrent", "LeakCurrent"),
        "hasAlarm": bool(alarms),
        "alarms": _bounded_json(alarms),
    }


def _lamp_status_matches(
    item: dict,
    *,
    controller_state: str,
    lamp_state: str,
    alarm_only: bool,
) -> bool:
    if controller_state != "all" and item.get("controllerState") != controller_state:
        return False
    if lamp_state == "abnormal" and item.get("hasAlarm") is not True:
        return False
    if lamp_state in {"on", "off"} and item.get("lampState") != lamp_state:
        return False
    if alarm_only and item.get("hasAlarm") is not True:
        return False
    return True


def _normalize_lamp_alarm(item: dict) -> dict:
    state_code = _smartlight_int(item.get("alarmState"))
    state_label = _first(item, "alarmStateName") or {
        0: "当前报警",
        1: "非当前报警",
        2: "已解除报警",
        3: "已处置",
    }.get(state_code, "未知")
    return {
        "id": _first(item, "hisHitchAlarmId", "alarmId", "id"),
        "occurredAt": _first(item, "alarmAddDate", "occurDate"),
        "lastActivityAt": _first(item, "lastDate", "alarmAddDate"),
        "lampPostId": _first(item, "lampPostId"),
        "lampPost": _first(item, "lampPostCode", "lampPostName"),
        "lampId": _first(item, "aloneLampId"),
        "lamp": _first(item, "lampEffectName", "aloneLampName"),
        "roadId": _first(item, "streetId"),
        "road": _first(item, "streetName", "roadName"),
        "side": _first(item, "streetSideName"),
        "cabinetCode": _first(item, "controlCabinetCode"),
        "cabinet": _first(item, "controlCabinetName"),
        "workArea": _first(item, "workAreaName"),
        "alarmTypeId": _first(item, "hitchDicId"),
        "alarmType": _first(item, "hitchDicName", "alarmTypeName"),
        "stateCode": state_code,
        "stateLabel": state_label,
        "leakageVoltage": _first(item, "leakageVoltage"),
        "leakageCurrent": _first(item, "leakageCurrent"),
        "remark": _first(item, "remark"),
    }


def _resolve_survey_time_range(
    arguments: dict,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    start_text = str(arguments.get("start_time") or "").strip()
    end_text = str(arguments.get("end_time") or "").strip()
    if bool(start_text) != bool(end_text):
        raise ValueError("start_time and end_time must be provided together")
    if not start_text:
        end = (now or datetime.now(_SMARTLIGHT_BUSINESS_TIMEZONE)).astimezone(
            _SMARTLIGHT_BUSINESS_TIMEZONE
        )
        start = end - timedelta(hours=24)
        source = "default_last_24_hours"
    else:
        start = _parse_business_datetime(start_text, "start_time")
        end = _parse_business_datetime(end_text, "end_time")
        source = "explicit"
    if start > end:
        raise ValueError("start_time cannot be after end_time")
    if end - start > timedelta(days=_SMARTLIGHT_SURVEY_MAX_DAYS):
        raise ValueError("RTU survey range cannot exceed 7 days")
    return (
        start.strftime("%Y-%m-%d %H:%M"),
        end.strftime("%Y-%m-%d %H:%M"),
        source,
    )


def _resolve_bounded_date_range(
    arguments: dict,
    *,
    default_days: int,
    maximum_days: int,
    range_name: str,
    now: datetime | None = None,
) -> tuple[str, str, str, int | None]:
    start_text = str(arguments.get("start_date") or "").strip()
    end_text = str(arguments.get("end_date") or "").strip()
    raw_last_days = arguments.get("last_days")
    if raw_last_days not in (None, "") and (start_text or end_text):
        raise ValueError("last_days cannot be combined with start_date or end_date")
    business_now = (now or datetime.now(_SMARTLIGHT_BUSINESS_TIMEZONE)).astimezone(
        _SMARTLIGHT_BUSINESS_TIMEZONE
    )
    if raw_last_days not in (None, ""):
        last_days = _bounded_int(
            raw_last_days,
            default=default_days,
            minimum=1,
            maximum=maximum_days,
        )
        end = business_now.date()
        start = end - timedelta(days=last_days - 1)
        return start.isoformat(), end.isoformat(), "last_days", last_days
    if not start_text and not end_text:
        end = business_now.date()
        start = end - timedelta(days=default_days - 1)
        return start.isoformat(), end.isoformat(), "default_last_days", default_days
    if bool(start_text) != bool(end_text):
        raise ValueError("start_date and end_date must be provided together")
    start = datetime.strptime(_optional_date(start_text, "start_date"), "%Y-%m-%d").date()
    end = datetime.strptime(_optional_date(end_text, "end_date"), "%Y-%m-%d").date()
    if start > end:
        raise ValueError("start_date cannot be after end_date")
    if (end - start).days + 1 > maximum_days:
        raise ValueError(f"{range_name} range cannot exceed {maximum_days} days")
    return start.isoformat(), end.isoformat(), "explicit", None


def _resolve_bounded_time_range(
    arguments: dict,
    *,
    default_hours: int,
    maximum_days: int,
    range_name: str,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    start_text = str(arguments.get("start_time") or "").strip()
    end_text = str(arguments.get("end_time") or "").strip()
    if bool(start_text) != bool(end_text):
        raise ValueError("start_time and end_time must be provided together")
    if not start_text:
        end = (now or datetime.now(_SMARTLIGHT_BUSINESS_TIMEZONE)).astimezone(
            _SMARTLIGHT_BUSINESS_TIMEZONE
        )
        start = end - timedelta(hours=default_hours)
        source = f"default_last_{default_hours}_hours"
    else:
        start = _parse_business_datetime(start_text, "start_time")
        end = _parse_business_datetime(end_text, "end_time")
        source = "explicit"
    if start > end:
        raise ValueError("start_time cannot be after end_time")
    if end - start > timedelta(days=maximum_days):
        raise ValueError(f"{range_name} range cannot exceed {maximum_days} days")
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
        source,
    )


def _parse_business_datetime(value: str, field_name: str) -> datetime:
    normalized = value.replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, pattern)
            return parsed.replace(tzinfo=_SMARTLIGHT_BUSINESS_TIMEZONE)
        except ValueError:
            continue
    raise ValueError(f"{field_name} must use YYYY-MM-DD HH:MM")


def _normalize_rtu_survey(item: dict) -> dict:
    received_at = _first(item, "addDateTime", "copDate", "addDate")
    if not received_at and item.get("addDate") and item.get("addTime"):
        received_at = f"{item['addDate']} {item['addTime']}"
    return {
        "id": _first(item, "hisCoplogPhaseId", "coplogId", "id"),
        "rtuId": _first(item, "rtuId"),
        "rtuCode": _first(item, "rtuCode"),
        "rtuName": _first(item, "rtuName"),
        "receivedAt": received_at,
        "rtuTime": _first(item, "rtuDateTime", "rtuTime", "deviceTime"),
        "phaseVoltage": {
            "a": _first(item, "strRtuScaleU1", "rtuScaleU1", "Ua", "ua"),
            "b": _first(item, "strRtuScaleU2", "rtuScaleU2", "Ub", "ub"),
            "c": _first(item, "strRtuScaleU3", "rtuScaleU3", "Uc", "uc"),
        },
        "phaseCurrent": {
            "a": _first(item, "strRtuScaleIsp1", "Ia", "ia"),
            "b": _first(item, "strRtuScaleIsp2", "Ib", "ib"),
            "c": _first(item, "strRtuScaleIsp3", "Ic", "ic"),
        },
        "phaseCurrentRatio": {
            "a": _first(item, "iaIan", "strRtuScaleI1", "Ian", "ian"),
            "b": _first(item, "ibIbn", "strRtuScaleI2", "Ibn", "ibn"),
            "c": _first(item, "icIcn", "strRtuScaleI3", "Icn", "icn"),
        },
        "powerFactor": {
            "a": _first(item, "APowerFactor", "powerFactorA"),
            "b": _first(item, "BPowerFactor", "powerFactorB"),
            "c": _first(item, "CPowerFactor", "powerFactorC"),
        },
        "temperature": _first(item, "temperature"),
        "humidity": _first(item, "humidity"),
        "relayCurrents": _bounded_json(
            _first(item, "jsonRelayIsp", "relayCurrents", "relayIsps")
        ),
        "circuitCurrents": _bounded_json(
            _first(item, "jsonRoadIsp", "roadIsps")
        ),
        "leakCurrents": _bounded_json(
            _first(item, "LeakCurrents", "relayLeakCurrents", "leakCurrents")
        ),
        "relayLeakIds": _bounded_json(item.get("relayLeakIds")),
        "openRelays": _bounded_json(_first(item, "onRelayIds", "openRelayIds")),
        "closedRelays": _bounded_json(_first(item, "offRelayIds", "closedRelayIds")),
        "phaseCircuits": {
            "a": _first(item, "roadInA"),
            "b": _first(item, "roadInB"),
            "c": _first(item, "roadInC"),
        },
        "state": _survey_state_label(item),
        "stateCode": _smartlight_int(_first(item, "isSucceeded", "state")),
        "alarmContent": _first(item, "hitchIntro", "alarmContent", "hitchContent")
        or ("正常" if _smartlight_int(item.get("isSucceeded")) == 1 else None),
    }


def _normalize_energy_record(item: dict) -> dict:
    values = item.get("item") if isinstance(item.get("item"), dict) else {}
    period_values = [
        {
            "date": str(date),
            "value": None if value in (None, "", "--") else value,
            "rawValue": value,
        }
        for date, value in sorted(values.items(), key=lambda pair: str(pair[0]))
    ]
    return {
        "deviceId": _first(item, "ID", "rtuId", "id"),
        "deviceCode": _first(item, "code", "rtuCode", "deviceCode"),
        "deviceName": _first(item, "name", "rtuName", "deviceName"),
        "productModel": _first(item, "productModel", "rtuModel", "model"),
        "reportDate": _first(item, "reportDate"),
        "unit": _first(item, "unit", "unitName", "energyUnit"),
        "periodValues": period_values,
        "downstreamSummary": {
            "total": _first(item, "total"),
            "average": _first(item, "avg"),
            "samePeriodAverage": _first(item, "savg"),
            "realElectricity": _first(item, "realElectDegree"),
            "theoreticalElectricity": _first(item, "theoryElectDegree"),
            "savedElectricity": _first(item, "saveElectDegree"),
            "savingRate": _first(item, "saveSlectDegree", "avgRate"),
        },
        "rtuEmpty": _smartlight_bool(item.get("rtuEmpty")),
    }


def _energy_warnings(items: list[dict]) -> list[str]:
    warnings: list[str] = []
    if items and not any(item.get("unit") not in (None, "") for item in items):
        warnings.append("用电量页面响应未明确返回单位，结果保留原值且不猜测单位。")
    missing = sum(
        1
        for item in items
        for point in item.get("periodValues") or []
        if point.get("value") is None
    )
    if missing:
        warnings.append(f"下游以空值或 -- 返回了 {missing} 个缺失数据点。")
    return warnings


def _normalize_lamp_survey(item: dict) -> dict:
    alarms = item.get("hitchAlarms") if isinstance(item.get("hitchAlarms"), list) else []
    return {
        "id": _first(item, "hisCoplogId", "coplogId", "id"),
        "lampPostId": _first(item, "lampPostId", "LampPostID"),
        "lampPostCode": _first(item, "lampPostCode", "LampPostCode"),
        "lampPostName": _first(item, "lampPostName", "LampPostName"),
        "lampId": _first(item, "aloneLampId", "lampId"),
        "lampName": _first(item, "lampEffectName", "aloneLampName", "lampName"),
        "controllerId": _first(item, "aloneLampControlId", "controllerId"),
        "road": _first(item, "streetName", "roadName"),
        "cabinet": _first(item, "controlCabinetName", "cabinetName"),
        "rtu": _first(item, "rtuName", "rtuCode"),
        "deviceTime": _first(item, "deviceTime", "lampDateTime", "rtuDateTime"),
        "receivedAt": _first(item, "copDate", "addDateTime", "addDate"),
        "controllerOnline": _smartlight_bool(_first(item, "IsOnline", "isOnline", "online")),
        "switchOn": _smartlight_bool(_first(item, "IsSwitchOn", "isSwitchOn", "switchOn")),
        "voltage": _first(item, "U", "voltage", "lampU"),
        "current": _first(item, "I", "current", "lampIsp"),
        "activePower": _first(item, "Ap", "activePower"),
        "powerFactor": _first(item, "Pf", "powerFactor"),
        "energy": _first(item, "energy", "electricDegree"),
        "dimming": _first(item, "dimmingValue", "dimming"),
        "alarmContent": _first(item, "hitchIntro", "alarmContent"),
        "hasAlarm": bool(alarms),
        "alarms": _bounded_json(alarms),
    }


def _normalize_rtu_leakage_alarm(item: dict) -> dict:
    base = _normalize_alarm(item)
    return {
        "id": base.get("id"),
        "rtuId": _first(item, "rtuId"),
        "rtuCode": _first(item, "rtuCode", "deviceCode"),
        "rtuName": _first(item, "rtuName", "deviceName", "controlCabinetName"),
        "cabinet": _first(item, "controlCabinetName", "cabinetName"),
        "branchId": _first(item, "relayId", "roadId", "branchId"),
        "branch": _first(item, "relayName", "roadName", "branchName", "relayCode"),
        "leakageCurrent": _first(
            item,
            "leakageCurrent",
            "relayLeakCurrent",
            "leakCurrent",
            "currentValue",
        ),
        "leakageUnit": _first(item, "leakageUnit", "currentUnit", "unit"),
        "threshold": _first(item, "leakageThreshold", "threshold", "alarmValue"),
        "occurredAt": base.get("occurredAt"),
        "lastActivityAt": base.get("lastActivityAt"),
        "clearedAt": _first(item, "clearDate", "recoverTime", "endTime"),
        "stateCode": base.get("stateCode"),
        "stateLabel": base.get("stateLabel"),
        "alarmType": base.get("type"),
        "message": base.get("message"),
        "remark": _first(item, "remark"),
        "workArea": base.get("workArea"),
        "workAreaSubmitted": base.get("workAreaSubmitted"),
        "workAreaSubmitState": base.get("workAreaSubmitState"),
    }


def _rtu_leakage_warnings(items: list[dict]) -> list[str]:
    warnings: list[str] = []
    if items and not any(item.get("leakageUnit") not in (None, "") for item in items):
        warnings.append("RTU 支路漏电响应未明确返回电流单位，结果不猜测单位。")
    if items and not any(item.get("threshold") not in (None, "") for item in items):
        warnings.append("下游记录未返回报警阈值，结果不自行推断阈值。")
    return warnings


def _normalize_off_hours_current(item: dict) -> dict:
    return {
        "id": _first(item, "hisCoplogPhaseId", "coplogId", "id"),
        "rtuId": _first(item, "rtuId"),
        "rtuCode": _first(item, "rtuCode", "deviceCode"),
        "rtuName": _first(item, "rtuName", "deviceName"),
        "cabinet": _first(item, "controlCabinetName", "cabinetName"),
        "branchId": _first(item, "relayId", "roadId", "branchId"),
        "branch": _first(item, "relayName", "roadName", "branchName"),
        "observedAt": _first(item, "addDateTime", "copDate", "deviceTime"),
        "current": _first(item, "current", "relayCurrent", "relayIsp", "avgCurrent"),
        "averageCurrent": _first(item, "avgCurrent", "averageCurrent"),
        "maximumCurrent": _first(item, "maxCurrent", "maximumCurrent"),
        "unit": _first(item, "currentUnit", "unit"),
        "downstreamRating": _first(item, "rating"),
    }


def _normalize_inspection_log_summary(item: dict) -> dict:
    return {
        "groupId": _first(item, "groupId"),
        "groupName": _first(item, "groupName"),
        "shouldChecked": _smartlight_int(item.get("shouldChecked")),
        "actuallyChecked": _smartlight_int(_first(item, "reality", "actuallyChecked")),
        "normal": _smartlight_int(item.get("normal")),
        "abnormal": _smartlight_int(item.get("abnormal")),
        "levelCounts": {
            "statistical": _smartlight_int(item.get("statisLevel")),
            "level1": _smartlight_int(item.get("oneLevel")),
            "level2": _smartlight_int(item.get("twoLevel")),
            "level3": _smartlight_int(item.get("threeLevel")),
        },
        "isTotalRow": str(item.get("groupName") or "").strip() == "合计",
    }


def _normalize_maintenance_record(item: dict) -> dict:
    raw_type = _smartlight_int(item.get("deviceType"))
    device_type = {1: "rtu", 2: "lamppost"}.get(raw_type, "unknown")
    return {
        "recordId": _first(item, "id", "overhaulId"),
        "deviceId": _first(item, "deviceId"),
        "deviceCode": _first(item, "deviceCode"),
        "deviceType": device_type,
        "deviceTypeCode": raw_type,
        "overhauledAt": _first(item, "overhaulTimeStr", "overhaulTime"),
        "overhaulUserId": _first(item, "overhaulUserId"),
        "overhaulUserName": _first(item, "overhaulUserName", "overhaulUser"),
        "road": _first(item, "streetName", "roadName"),
        "location": {
            "longitude": _first(item, "wgs84xTude", "longitude"),
            "latitude": _first(item, "wgs84yTude", "latitude"),
        },
        "stableDetailAvailable": False,
    }


def _standard_page_result(
    *,
    scope: str,
    filters: dict,
    page: int,
    size: int,
    payload: Any,
    items: list[dict],
    source_page: str,
    source_kind: str,
    warnings: list[str],
    extra: dict | None = None,
) -> dict:
    result = {
        "scope": scope,
        "filters": filters,
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "timezone": _SMARTLIGHT_BUSINESS_TIMEZONE_NAME,
        "page": page,
        "size": size,
        "downstreamTotal": _page_total(payload),
        "returnedCount": len(items),
        "count": len(items),
        "truncated": False,
        "sourcePage": source_page,
        "sourceKind": source_kind,
        "warnings": warnings,
        "items": items,
    }
    if extra:
        result.update(extra)
    return result


def _smartlight_number(value: Any) -> float | None:
    if value in (None, "", "--") or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _rounded_number(value: float) -> int | float:
    rounded = round(value, 6)
    return int(rounded) if rounded.is_integer() else rounded


def _survey_state_label(item: dict) -> Any:
    explicit = _first(item, "stateName", "workState", "AlarmStatus", "state")
    if explicit not in (None, ""):
        return explicit
    succeeded = _smartlight_int(item.get("isSucceeded"))
    if succeeded == 1:
        return "正常"
    if succeeded == 0:
        return "异常"
    return None


def _smartlight_report_result(
    *,
    report_type: str,
    title: str,
    columns: tuple[tuple[str, str], ...],
    rows: list[dict],
    metadata: dict,
) -> dict:
    bounded_rows = rows[:_SMARTLIGHT_ANALYSIS_LIMIT]
    selected_metadata = dict(metadata)
    selected_metadata["exportedCount"] = len(bounded_rows)
    selected_metadata["analysisLimit"] = _SMARTLIGHT_ANALYSIS_LIMIT
    selected_metadata["truncated"] = bool(
        selected_metadata.get("truncated") or len(rows) > len(bounded_rows)
    )
    return {
        "reportType": report_type,
        "reportTitle": title,
        "filenameStem": title,
        "columns": [
            {"key": key, "label": label}
            for key, label in columns
        ],
        "rows": bounded_rows,
        "metadata": selected_metadata,
    }


def _normalize_asset_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"cabinet", "rtu", "lamppost"}:
        raise ValueError("asset_type must be cabinet, rtu, or lamppost")
    return normalized


def _normalize_choice(
    value: Any,
    *,
    default: str,
    allowed: set[str],
    field_name: str,
) -> str:
    normalized = str(value or default).strip().lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {choices}")
    return normalized


def _resolve_analysis_date_range(arguments: dict) -> tuple[str, str, str, int | None]:
    if not any(
        arguments.get(name) not in (None, "")
        for name in ("start_date", "end_date", "last_days")
    ):
        start, end, _source, days = _resolve_date_range({"last_days": 30})
        return start, end, "default_last_days", days
    return _resolve_date_range(arguments)


def _optional_date(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from exc
    return normalized


def _json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _lamppost_filters(context: dict, keyword: str) -> dict:
    return {
        "labels": [],
        "_like_params": keyword,
        "_like_cabinet": "",
        "_like_imei": "",
        "_include_streetId": [],
        "_include_controlCabinetId": [],
        "_include_lampPostTypeId": None,
        "_include_materialTypeId": [],
        "_include_workAreaId": [],
        "_include_streetSideId": [],
        "_timebegin_addDate": "",
        "_timeend_addDate": "",
        "oddEvenNumber": None,
        "userId": context["userId"],
    }


def _resolve_date_range(
    arguments: dict,
    *,
    now: datetime | None = None,
) -> tuple[str, str, str, int | None]:
    start_date = str(arguments.get("start_date") or "").strip()
    end_date = str(arguments.get("end_date") or "").strip()
    raw_last_days = arguments.get("last_days")
    if raw_last_days is not None and (start_date or end_date):
        raise ValueError("last_days cannot be combined with start_date or end_date")
    if raw_last_days is not None:
        last_days = _bounded_int(
            raw_last_days,
            default=30,
            minimum=1,
            maximum=3660,
        )
        business_now = (now or datetime.now(_SMARTLIGHT_BUSINESS_TIMEZONE)).astimezone(
            _SMARTLIGHT_BUSINESS_TIMEZONE
        )
        end = business_now.date()
        start = end - timedelta(days=last_days - 1)
        return start.isoformat(), end.isoformat(), "last_days", last_days
    for name, value in (("start_date", start_date), ("end_date", end_date)):
        if not value:
            continue
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date cannot be after end_date")
    return start_date, end_date, "explicit" if (start_date or end_date) else "none", None


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("pagination value must be an integer") from exc
    if normalized < minimum or normalized > maximum:
        raise ValueError(f"pagination value must be between {minimum} and {maximum}")
    return normalized


def _page_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for name in ("list", "data", "rows", "content", "items"):
        items = payload.get(name)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _page_total(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return 0
    for name in ("totalCount", "total", "records", "count"):
        try:
            if payload.get(name) is not None:
                return int(payload[name])
        except (TypeError, ValueError):
            continue
    return len(_page_items(payload))


def _result_record(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return next((item for item in result if isinstance(item, dict)), None)
    if any(key in payload for key in ("id", "lampPostId", "lampPostID")):
        return payload
    return None


def _selected_fields(payload: Any, names: tuple[str, ...]) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {name: _bounded_json(payload[name]) for name in names if name in payload}


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_bounded_json(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _bounded_json(item, depth=depth + 1)
            for key, item in list(value.items())[:30]
        }
    return str(value)[:200]


def _first(item: dict, *names: str) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _asset_id(asset_type: str, item: dict) -> Any:
    if asset_type == "cabinet":
        return _first(item, "controlCabinetId", "id")
    if asset_type == "rtu":
        return _first(item, "rtuId", "id")
    return _first(item, "lampPostID", "lampPostId", "id")


def _normalize_asset_summary(asset_type: str, item: dict) -> dict:
    if asset_type == "cabinet":
        return {
            **_normalize_cabinet(item),
            "assetType": asset_type,
            "road": _first(item, "streetName", "roadName"),
            "side": _first(item, "streetSideName", "sideName"),
            "workArea": _first(item, "workAreaName"),
            "address": _first(item, "address", "controlCabinetAddress"),
            "capacity": _first(item, "capacityStr", "capacity"),
            "electricalType": _first(item, "electricalTypeName", "electricalType"),
        }
    if asset_type == "rtu":
        return {
            "assetType": asset_type,
            "id": _first(item, "rtuId", "id"),
            "code": _first(item, "rtuCode", "code"),
            "name": _first(item, "rtuName", "name"),
            "model": _first(item, "productModel", "modelName", "rtuModel"),
            "type": _first(item, "rtuTypeName", "typeName"),
            "cabinet": _first(item, "controlCabinetName", "cabinetName"),
            "group": _first(item, "groupName", "rtuGroupName"),
            "workArea": _first(item, "workAreaName"),
            "state": _first(
                item,
                "runningStateName",
                "runStateName",
                "onlineState",
                "state",
            ),
        }
    return {**_normalize_lamppost(item), "assetType": asset_type}


def _missing_asset_detail(asset_type: str, asset_id: str) -> dict:
    return {
        "assetType": asset_type,
        "assetId": asset_id,
        "found": False,
        "detail": None,
    }


def _normalize_cabinet_detail(item: dict) -> dict:
    return {
        **_normalize_asset_summary("cabinet", item),
        "transformerType": _first(item, "transTypeName", "transformerTypeName"),
        "electricityNumber": _first(item, "electricityNumber"),
        "organization": _first(item, "organName", "organizationName"),
        "longitude": _first(item, "longitude", "lng", "lon"),
        "latitude": _first(item, "latitude", "lat"),
        "createdAt": _first(item, "addDate", "createdAt"),
        "remark": _first(item, "remark"),
    }


def _normalize_rtu_detail(item: dict) -> dict:
    return {
        **_normalize_asset_summary("rtu", item),
        "installDate": _first(item, "installDate", "installationDate"),
        "outDate": _first(item, "outDate", "productionDate"),
        "warrantyUntil": _first(item, "warrantyDate", "warrantyUntil"),
        "onlineAt": _first(item, "onlineTime", "lastOnlineTime"),
        "manufacturer": _first(item, "manufacturerName", "factoryName"),
        "createdAt": _first(item, "addDate", "createdAt"),
        "remark": _first(item, "remark"),
    }


def _normalize_lamppost_detail(item: dict) -> dict:
    return {
        **_normalize_asset_summary("lamppost", item),
        "type": _first(item, "lampPostTypeName", "LampPostTypeName", "typeName"),
        "height": _first(item, "lampPostHeight", "height"),
        "material": _first(item, "materialTypeName", "materialName"),
        "rtu": _first(item, "rtuName"),
        "rtuCode": _first(item, "rtuCode"),
        "transformer": _first(item, "transformerName", "transName"),
        "address": _first(item, "address", "lampPostAddress"),
        "longitude": _first(item, "longitude", "lng", "lon"),
        "latitude": _first(item, "latitude", "lat"),
        "createdAt": _first(item, "addDate", "createdAt"),
        "remark": _first(item, "remark"),
    }


def _normalize_rtu_relay(item: dict) -> dict:
    switches: list[dict] = []
    circuit_count = 0
    for switch in item.get("roadSwitchList") or []:
        if not isinstance(switch, dict):
            continue
        circuits = []
        for circuit in switch.get("rRturoadList") or []:
            if not isinstance(circuit, dict):
                continue
            circuits.append(
                {
                    "id": _first(circuit, "rturoadId", "id"),
                    "number": _first(circuit, "rturoadNumber", "roadNumber"),
                    "name": _first(circuit, "rturoadName", "roadName"),
                    "phase": _first(circuit, "powerType", "powerTypeName"),
                    "lampPostCount": _first(circuit, "lamppostCount"),
                    "lampCount": _first(circuit, "lampCount"),
                }
            )
        circuit_count += len(circuits)
        switches.append(
            {
                "id": _first(switch, "roadSwitchId", "id"),
                "number": _first(switch, "roadSwitchNumber"),
                "model": _first(switch, "switchModel"),
                "circuits": circuits,
            }
        )
    return {
        "id": _first(item, "rturelayId", "id"),
        "number": _first(item, "rturelayNumber"),
        "name": _first(item, "rturelayName"),
        "enabled": _first(item, "isEnabled"),
        "workMode": _first(item, "workModelName"),
        "externalPower": _first(item, "externalPowerFlag"),
        "remark": _first(item, "relayRemark", "remark"),
        "roadSwitchCount": len(switches),
        "circuitCount": circuit_count,
        "roadSwitches": switches,
    }


def _normalize_cabinet(item: dict) -> dict:
    return {
        "id": _first(item, "controlCabinetId", "id"),
        "code": _first(item, "controlCabinetCode", "code"),
        "name": _first(item, "controlCabinetName", "name"),
        "state": _first(item, "onlineState", "rtuState", "state"),
        "alarmState": item.get("alarmState"),
    }


def _normalize_lamppost(item: dict) -> dict:
    return {
        "id": _first(item, "lampPostID", "lampPostId", "id"),
        "code": _first(item, "LampPostCode", "lampPostCode", "code"),
        "name": _first(item, "lampPostName", "name"),
        "road": _first(item, "StreetName", "streetName", "roadName"),
        "side": _first(item, "StreetSideName", "streetSideName"),
        "cabinet": _first(item, "controlCabinetName", "cabinetName"),
        "workArea": _first(item, "WorkAreaName", "workAreaName"),
        "lampCount": _first(item, "aloneLampCount", "lampCount"),
        "state": _first(item, "onlineState", "state", "lampPostState"),
    }


def _normalize_alarm(item: dict) -> dict:
    occurred_at = _first(
        item,
        "occurDate",
        "alarmTime",
        "hitchTime",
        "createTime",
    )
    last_activity_at = _first(item, "lastDate", "lastTime") or occurred_at
    state_code = _first(item, "conductStatue", "alarmState", "dealState")
    if isinstance(state_code, str) and state_code.strip().isdigit():
        state_code = int(state_code.strip())
    state_label = _first(item, "alarmStateName")
    if not state_label:
        normalized_state_code = _smartlight_int(state_code)
        state_label = _smartlight_alarm_state_label(normalized_state_code)
        if state_label == "未知状态" and state_code not in (None, ""):
            state_label = f"未知状态（代码 {state_code}）"
    work_area_submit_state = _smartlight_int(item.get("isSubmitWorkArea"))
    if work_area_submit_state is None and item.get("isSubmitWorkArea") in (None, ""):
        work_area_submit_state = 0
    return {
        "id": _first(item, "hitchAlarmId", "alarmId", "id"),
        "occurredAt": occurred_at,
        "lastActivityAt": last_activity_at,
        "time": occurred_at,
        "lastTime": last_activity_at,
        "device": _first(
            item,
            "controlCabinetName",
            "rtuName",
            "deviceName",
        ),
        "deviceCode": _first(item, "rtuCode", "controlCabinetCode", "deviceCode"),
        "type": _first(
            item,
            "hitchName",
            "alarmTypeName",
            "hitchTypeName",
            "alarmName",
        ),
        "level": _first(item, "alarmLevelName", "alarmLevel", "weightFacto"),
        "state": state_label,
        "stateCode": state_code,
        "stateLabel": state_label,
        "message": _first(
            item,
            "hitchIntro",
            "alarmContent",
            "hitchContent",
            "description",
        ),
        "alarmWeight": _smartlight_int(item.get("weightFacto")),
        "workAreaId": _first(item, "workAreaId"),
        "workArea": _first(item, "workAreaName"),
        "workAreaSubmitted": (
            work_area_submit_state == 1
            if work_area_submit_state is not None
            else None
        ),
        "workAreaSubmitState": work_area_submit_state,
        "workAreaSubmitStateLabel": _smartlight_work_area_state_label(
            work_area_submit_state
        ),
        "group": _first(item, "groupName"),
    }


def _normalize_inspection_task(item: dict) -> dict:
    state_code, state_label = _inspection_state(item)
    confirmed_device_count = _first(item, "confirmDeviceNum")
    lamp_post_count = _first(item, "lampostQty", "lampPostQty")
    rtu_count = _first(item, "rtuQty")
    return {
        "id": _first(item, "inspectionTaskId", "taskId", "id"),
        "taskName": _first(item, "taskName", "inspectionTaskName"),
        "planName": _first(item, "planName", "inspectionPlanName"),
        "state": state_label or state_code,
        "stateCode": state_code,
        "stateLabel": state_label,
        "assignee": _first(item, "inspectionUserName", "taskUserName", "userName"),
        "inspectionGroup": _first(item, "groupName", "inspectionGroupName"),
        "startTime": _first(
            item,
            "startTime",
            "planStartTime",
            "taskStartDate",
        ),
        "endTime": _first(
            item,
            "endTime",
            "planEndTime",
            "taskDeadline",
        ),
        "progress": _first(item, "taskProgress", "progress"),
        "yesterdayProgress": _first(
            item,
            "yesterdayTaskProgress",
            "yesterdayProgress",
        ),
        "confirmedDeviceCount": confirmed_device_count,
        "lampPostCount": lamp_post_count,
        "rtuCount": rtu_count,
        "deviceCounts": {
            "confirmed": confirmed_device_count,
            "lampPosts": lamp_post_count,
            "rtus": rtu_count,
        },
        "progressScope": "downstream_reported_independent_metric",
        "cycle": _first(item, "planCycleName"),
        "batch": _first(item, "planBatch"),
        "createdAt": _first(item, "addTime", "createdAt"),
        "createdBy": _first(item, "addUser", "createdBy"),
    }


def _normalize_inspection_day(item: dict) -> dict:
    return {
        "groupId": _first(item, "groupId", "inspectionDeviceGroupId"),
        "date": _date_part(
            _first(item, "dateTimeStr", "inspectionDate", "dateTime", "date")
        ),
        "plannedDeviceCount": _first(item, "deviceNum", "plannedDeviceNum"),
        "completedDeviceCount": _first(item, "realityNum", "completedDeviceNum"),
        "completionRate": _first(item, "rate", "completionRate"),
    }


def _normalize_inspection_clockin(item: dict) -> dict:
    has_issues = _first(item, "hasIssues", "isProblem", "hasProblem")
    if has_issues in (0, "0", "false", "False"):
        has_issues = False
    elif has_issues in (1, "1", "true", "True"):
        has_issues = True
    return {
        "id": _first(item, "clockinId", "inspectionClockinId", "id"),
        "recordedAt": _first(
            item,
            "clockinTime",
            "clockInTime",
            "inspectionTime",
            "addTime",
        ),
        "recorder": _first(
            item,
            "clockinUserName",
            "clockInUserName",
            "inspectionUserName",
            "userName",
        ),
        "deviceId": _first(item, "deviceId", "facilityId"),
        "deviceCode": _first(
            item,
            "deviceCode",
            "lampPostCode",
            "rtuCode",
            "controlCabinetCode",
        ),
        "deviceName": _first(
            item,
            "deviceName",
            "lampPostName",
            "rtuName",
            "controlCabinetName",
        ),
        "deviceType": _first(item, "deviceTypeName", "facilityTypeName", "deviceType"),
        "position": _first(item, "clockinAddress", "position", "address"),
        "hasIssues": has_issues,
        "issueDescription": _first(
            item,
            "issuesDescription",
            "issueDescription",
            "problemDescription",
            "remark",
        ),
    }


def _inspection_state(item: dict) -> tuple[object, object]:
    raw_code = _first(item, "taskState", "state")
    raw_label = _first(item, "taskStateName")
    state_code = raw_code
    if isinstance(raw_code, str) and raw_code.strip().isdigit():
        state_code = int(raw_code.strip())
    state_labels = {
        1: "待执行",
        2: "执行中",
    }
    return state_code, raw_label or state_labels.get(state_code)


def _normalize_inspection_state_filter(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Smartlight inspection state must be a numeric state code")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        labels = {
            "待执行": 1,
            "执行中": 2,
        }
        if normalized in labels:
            return labels[normalized]
        if normalized.isdigit():
            return int(normalized)
    raise ValueError(
        "Smartlight inspection state must be a numeric state code, 待执行, or 执行中"
    )


def _normalize_leakage(item: dict) -> dict:
    return {
        "id": _first(item, "hisHitchAlarmId", "coplogId", "alarmId", "id"),
        "time": _first(
            item,
            "alarmAddDate",
            "lastDate",
            "alarmTime",
            "operationTime",
            "createTime",
        ),
        "lampPost": _first(item, "lampPostName", "lampPostCode"),
        "lamp": _first(item, "aloneLampName", "lampName", "lampEffectName"),
        "road": _first(item, "streetName", "roadName"),
        "value": _first(
            item,
            "leakageCurrent",
            "electricLeakage",
            "leakageValue",
            "currentValue",
        ),
        "voltage": _first(item, "leakageVoltage", "voltage"),
        "alarmType": _first(item, "hitchDicName", "alarmTypeName"),
        "state": _first(item, "alarmStateName", "alarmState", "state"),
    }


def _count_values(items: list[dict], names: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = _first(item, *names)
        key = str(value if value not in (None, "") else "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _top_counts(items: list[dict], field: str, limit: int) -> list[dict]:
    counts = Counter(
        str(item[field])
        for item in items
        if item.get(field) not in (None, "")
    )
    return [
        {"value": value, "count": count}
        for value, count in counts.most_common(limit)
    ]


def _daily_counts(items: list[dict], field: str) -> list[dict]:
    counts = Counter(
        date
        for item in items
        if (date := _date_part(item.get(field)))
    )
    return [
        {"date": date, "count": counts[date]}
        for date in sorted(counts)
    ]


def _date_part(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if len(normalized) < 10:
        return None
    candidate = normalized[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Open a trusted field card for one exact RTU alarm, freeze the "
            "current remark and requested replacement, then require separate "
            "authorization. This step does not modify Smartlight."
        ),
        input_schema=SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-remark-update-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume an approved authorization, update the exact RTU alarm "
            "remark, and verify the result by authoritative readback."
        ),
        input_schema=SMARTLIGHT_ALARM_REMARK_UPDATE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-remark-update-commit-v1",
    ),
    required_scopes=frozenset({"smartlight:write:alarm_remark"}),
    field_schema=SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA,
    context_fields=("alarm_id",),
    prepare_function=prepare_smartlight_alarm_remark_update,
    commit_function=commit_smartlight_alarm_remark_update,
    contract_error=SmartlightAlarmRemarkContractMismatch,
    outcome_error=SmartlightAlarmRemarkOutcomeUnknown,
    field_message="请在可信字段卡中核对告警备注。",
    authorization_message="照明告警备注修改计划需要在可信授权卡中确认。",
)


SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Read one exact RTU alarm, validate work-area eligibility, freeze "
            "the target state, and require trusted authorization without a "
            "field card. This step does not modify Smartlight."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-work-area-submit-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume one approved authorization, submit the exact RTU alarm "
            "to its work area, and verify isSubmitWorkArea by readback."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-work-area-submit-commit-v1",
    ),
    required_scopes=frozenset({"smartlight:write:alarm_work_area_submit"}),
    field_schema=None,
    context_fields=("alarm_id",),
    prepare_function=prepare_smartlight_alarm_work_area_submit,
    commit_function=commit_smartlight_alarm_work_area_submit,
    contract_error=SmartlightAlarmActionContractMismatch,
    outcome_error=SmartlightAlarmActionOutcomeUnknown,
    field_message=None,
    authorization_message="请在可信授权卡中确认把该 RTU 告警提交工区。",
)


SMARTLIGHT_ALARM_WORK_AREA_REVOKE_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Read one exact submitted RTU alarm, freeze its work-area state, "
            "and require trusted authorization without a field card."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-work-area-revoke-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=SMARTLIGHT_ALARM_WORK_AREA_REVOKE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume one approved authorization, revoke the exact RTU "
            "alarm's work-area submission, and verify the result by readback."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="reversible_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-alarm-work-area-revoke-commit-v1",
    ),
    required_scopes=frozenset({"smartlight:write:alarm_work_area_revoke"}),
    field_schema=None,
    context_fields=("alarm_id",),
    prepare_function=prepare_smartlight_alarm_work_area_revoke,
    commit_function=commit_smartlight_alarm_work_area_revoke,
    contract_error=SmartlightAlarmActionContractMismatch,
    outcome_error=SmartlightAlarmActionOutcomeUnknown,
    field_message=None,
    authorization_message="请在可信授权卡中确认撤回该 RTU 告警的工区提交。",
)


SMARTLIGHT_RTU_ALARM_DISPOSE_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(
        name=SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY,
        version="0.1.0",
        description=(
            "Read one exact RTU alarm, validate that it is disposable, freeze "
            "the target state, and require explicit irreversible authorization."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_PREPARE_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-rtu-alarm-dispose-prepare-v1",
    ),
    commit_spec=CapabilitySpec(
        name=SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY,
        version="0.1.0",
        description=(
            "Consume one approved authorization, mark the exact RTU alarm "
            "as disposed, and require authoritative state-3 readback."
        ),
        input_schema=SMARTLIGHT_ALARM_ACTION_INPUT_SCHEMA,
        output_schema={"type": "object"},
        effect="controlled_write",
        adapter=SMARTLIGHT_ADAPTER_ID,
        workflow="smartlight-rtu-alarm-dispose-commit-v1",
    ),
    required_scopes=frozenset({"smartlight:write:alarm_disposition"}),
    field_schema=None,
    context_fields=("alarm_id",),
    prepare_function=prepare_smartlight_rtu_alarm_dispose,
    commit_function=commit_smartlight_rtu_alarm_dispose,
    contract_error=SmartlightAlarmActionContractMismatch,
    outcome_error=SmartlightAlarmActionOutcomeUnknown,
    field_message=None,
    authorization_message="该 RTU 告警处置不可撤销，请在可信授权卡中明确确认。",
)
