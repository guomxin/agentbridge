from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import time
from typing import Any, Callable
from urllib.parse import urljoin

from agentbridge.adapters.seeyon_submit_phases import pump_browser_events


WORKFLOW_REVOKE_PREPARE_CAPABILITY = "oa.workflow.revoke.prepare"
WORKFLOW_REVOKE_CAPABILITY = "oa.workflow.revoke"
WORKFLOW_REVOKE_CONTRACT_VERSION = "seeyon-workflow-revoke-v1"

WORKFLOW_REVOKE_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "affair_id": {"type": "string"},
        "repeal_comment": {"type": "string", "maxLength": 100},
        "input_submission_id": {"type": "string"},
    },
    "required": ["affair_id"],
    "additionalProperties": False,
}

WORKFLOW_REVOKE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"authorization_id": {"type": "string"}},
    "required": ["authorization_id"],
    "additionalProperties": False,
}

WORKFLOW_REVOKE_FIELD_CARD_SCHEMA = {
    "schema_version": "agentbridge.oa_workflow_revoke_fields.v1",
    "title": "填写流程撤销附言",
    "system": "致远 OA",
    "effect": "撤销一条已发流程并退回待发事项",
    "submit_label": "提交撤销附言",
    "notice": (
        "撤销是正式写操作，可能留下审计记录、通知当前处理人或触发表单业务逻辑。"
        "提交附言后还需在独立授权卡中核对目标流程；授权前不会执行撤销。"
    ),
    "fields": [
        {
            "name": "repeal_comment",
            "label": "撤销附言",
            "control": "textarea",
            "required": True,
            "max_length": 100,
            "rows": 4,
        }
    ],
}

_COLLECTION_CONTRACTS = {
    "sent": {
        "method": "listSent",
        "grid_id": "listSent",
        "manager_method": "getSentList",
    },
    "wait_send": {
        "method": "listWaitSend",
        "grid_id": "listWaitSend",
        "manager_method": "getWaitSendList",
    },
}


class WorkflowRevokeContractMismatch(RuntimeError):
    pass


class WorkflowRevokeOutcomeUnknown(RuntimeError):
    pass


def prepare_workflow_revoke(adapter, worker, arguments: dict) -> dict:
    inputs = normalize_workflow_revoke_inputs(arguments)
    row, page = _resolve_collection_row(
        adapter,
        worker,
        collection="sent",
        affair_id=inputs["affair_id"],
    )
    _validate_revoke_target(row)
    _check_revoke_eligibility(page, row)
    target = _frozen_target(row)
    return {
        "plan": {
            "schema_version": "agentbridge.oa_workflow_revoke_plan.v1",
            "business_intent": "revoke_sent_workflow",
            "target": target,
            "action_contract": {
                "version": WORKFLOW_REVOKE_CONTRACT_VERSION,
                "fingerprint": workflow_revoke_contract_fingerprint(),
                "selection_policy": "exactly_one_affair_id",
                "execution_entry": "cancelWorkFlow",
                "commit_entry": "colManager.transRepal",
                "verification": [
                    "sent_disappearance",
                    "wait_send_same_identity",
                    "wait_send_revoked_state",
                ],
            },
            "exact_input": {"repeal_comment": inputs["repeal_comment"]},
            "preconditions": {
                "sent_target_resolved": True,
                "target_identity_complete": True,
                "flow_not_finished": True,
                "oa_revoke_precheck_passed": True,
                "native_revoke_entry_present": True,
            },
            "expected_effect": {
                "workflow_revoked": True,
                "revoked_count": 1,
                "verification": "sent_to_wait_send_revoked_transition",
            },
        },
        "summary": workflow_revoke_summary(target, inputs["repeal_comment"]),
    }


def revoke_workflow(
    adapter,
    worker,
    plan: dict,
    *,
    enter_commit_boundary: Callable[[], None],
    timeout_seconds: float = 75,
) -> dict:
    _validate_revoke_plan(plan)
    target = dict(plan["target"])
    affair_id = _bounded_identifier(target.get("affair_id"), "affair_id")
    comment = _bounded_text(
        plan.get("exact_input", {}).get("repeal_comment"),
        "repeal_comment",
        100,
    )
    row, page = _resolve_collection_row(
        adapter,
        worker,
        collection="sent",
        affair_id=affair_id,
    )
    _assert_frozen_target(target, row)
    _validate_revoke_target(row)
    _check_revoke_eligibility(page, row)
    _select_exact_sent_row(page, row)
    dialog_frame = _open_revoke_dialog(page)
    _fill_revoke_comment(dialog_frame, comment)

    boundary_crossed = False
    with _readback_worker(worker) as readback_worker:
        enter_commit_boundary()
        boundary_crossed = True
        try:
            _confirm_revoke_dialog(page)
            verification = _wait_for_revoke_readback(
                adapter,
                readback_worker,
                target=target,
                action_page=page,
                timeout_seconds=timeout_seconds,
            )
            return {
                "schema_version": "agentbridge.oa_workflow_revoke_result.v1",
                "business_intent": "revoke_sent_workflow",
                "workflow_revoked": True,
                "revoked_count": 1,
                "target": {
                    "affair_id": affair_id,
                    "summary_id": target["summary_id"],
                    "process_id": target["process_id"],
                    "title": target["title"],
                },
                "verification": {
                    "confirmed": True,
                    "methods": [
                        "sent_disappearance",
                        "wait_send_same_identity",
                        "wait_send_revoked_state",
                    ],
                    "wait_send_state": verification,
                },
                "transport": "central_browser_session",
            }
        except WorkflowRevokeOutcomeUnknown:
            raise
        except BaseException as exc:
            if boundary_crossed:
                raise WorkflowRevokeOutcomeUnknown(
                    "The OA workflow revoke boundary was crossed, but verification failed."
                ) from exc
            raise


def normalize_workflow_revoke_inputs(arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("workflow revoke input must be an object")
    return {
        "affair_id": _bounded_identifier(arguments.get("affair_id"), "affair_id"),
        "repeal_comment": _bounded_text(
            arguments.get("repeal_comment"),
            "repeal_comment",
            100,
        ),
    }


def workflow_revoke_summary(target: dict, comment: str) -> dict:
    fields = [
        {"label": "事项", "value": str(target.get("title") or "")},
        {"label": "发起时间", "value": str(target.get("create_date") or "")},
        {"label": "当前待办人", "value": str(target.get("current_nodes") or "")},
        {"label": "撤销附言", "value": comment},
    ]
    return {
        "title": "撤销已发流程",
        "system": "致远 OA",
        "effect": "撤销后流程将终止当前流转并退回待发事项",
        "authorization_notice": (
            "授权后将立即执行一次撤销。该操作不可在 AgentBridge 中恢复，"
            "可能通知当前处理人并留下 OA 审计记录。"
        ),
        "authorize_label": "授权撤销流程",
        "fields": fields,
        "revoked_count": 1,
    }


def workflow_revoke_contract_fingerprint() -> str:
    contract = {
        "version": WORKFLOW_REVOKE_CONTRACT_VERSION,
        "selection_policy": "exactly_one_affair_id",
        "page_method": "listSent",
        "grid_manager_method": "getSentList",
        "execution_entry": "cancelWorkFlow",
        "dialog_method": "showRepealCommentDialog",
        "comment_limit": 100,
        "commit_entry": "colManager.transRepal",
        "verification": [
            "sent_disappearance",
            "wait_send_same_identity",
            "wait_send_revoked_state",
        ],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _resolve_collection_row(
    adapter,
    worker,
    *,
    collection: str,
    affair_id: str,
    required: bool = True,
):
    rows, page = _load_collection_rows(adapter, worker, collection=collection)
    matches = [row for row in rows if row.get("affair_id") == affair_id]
    if len(matches) == 1:
        return matches[0], page
    if not matches and not required:
        return None, page
    if not matches:
        raise WorkflowRevokeContractMismatch(
            f"The OA affair_id was not found in the {collection} collection."
        )
    raise WorkflowRevokeContractMismatch(
        f"The OA affair_id was not unique in the {collection} collection."
    )


def _load_collection_rows(adapter, worker, *, collection: str):
    try:
        contract = _COLLECTION_CONTRACTS[collection]
    except KeyError as exc:
        raise ValueError(f"unsupported revoke collection: {collection}") from exc
    adapter.list_templates(worker)
    page_url = urljoin(
        adapter.base_url,
        f"collaboration/collaboration.do?method={contract['method']}",
    )
    page = worker.goto(page_url, timeout_seconds=60)
    try:
        page.wait_for_function(
            "() => typeof window.colManager === 'function' "
            "&& typeof window.CallerResponder === 'function' "
            "&& window.grid && window.grid.grid",
            timeout=20000,
        )
        raw_rows = page.evaluate(
            _LOAD_COLLECTION_ROWS_SCRIPT,
            {
                "manager_method": contract["manager_method"],
                "page_size": 100,
                "max_pages": 20,
            },
        )
    except Exception as exc:
        raise WorkflowRevokeContractMismatch(
            f"The OA {collection} list no longer matches the registered grid contract."
        ) from exc
    if not isinstance(raw_rows, list):
        raise WorkflowRevokeContractMismatch(
            f"The OA {collection} grid did not return a row list."
        )
    return [_normalize_collection_row(row) for row in raw_rows if isinstance(row, dict)], page


def _normalize_collection_row(row: dict) -> dict:
    def text(name: str) -> str:
        return str(row.get(name) or "").strip()

    return {
        "affair_id": text("affairId"),
        "summary_id": text("summaryId"),
        "process_id": text("processId"),
        "title": text("subject"),
        "create_date": text("createDate") or text("startDate"),
        "current_nodes": text("currentNodesInfo"),
        "body_type": text("bodyType"),
        "template_id": text("templeteId"),
        "form_app_id": text("formAppId"),
        "form_record_id": text("formRecordid"),
        "flow_finished": row.get("flowFinished") is True
        or text("flowFinished").lower() == "true",
        "state": _optional_int(row.get("state")),
        "sub_state": _optional_int(row.get("subState")),
        "sub_state_name": text("subStateName"),
        "summary_state": _optional_int(row.get("summaryState")),
        "affair_state": _optional_int(row.get("affairState")),
    }


def _validate_revoke_target(row: dict) -> None:
    for name in ("affair_id", "summary_id", "process_id", "title"):
        if not str(row.get(name) or "").strip():
            raise WorkflowRevokeContractMismatch(
                f"The OA sent row is missing its stable {name}."
            )
    if row.get("flow_finished"):
        raise WorkflowRevokeContractMismatch(
            "The OA workflow is already finished and cannot be revoked."
        )
    if row.get("summary_state") not in (None, 0):
        raise WorkflowRevokeContractMismatch(
            "The OA workflow is not in an active sent state."
        )


def _check_revoke_eligibility(page, row: dict) -> None:
    try:
        result = page.evaluate(
            _REVOKE_PRECHECK_SCRIPT,
            {
                "affair_id": row["affair_id"],
                "summary_id": row["summary_id"],
            },
        )
    except Exception as exc:
        raise WorkflowRevokeContractMismatch(
            "The OA revoke eligibility contract could not be evaluated."
        ) from exc
    if not isinstance(result, dict) or result.get("ready") is not True:
        raise WorkflowRevokeContractMismatch(
            "The OA revoke entry or its eligibility checks are unavailable."
        )
    for key in ("secret_message", "repeal_message", "affair_message"):
        message = str(result.get(key) or "").strip()
        if message:
            raise WorkflowRevokeContractMismatch(message)


def _frozen_target(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "affair_id",
            "summary_id",
            "process_id",
            "title",
            "create_date",
            "current_nodes",
            "body_type",
            "template_id",
            "form_app_id",
            "form_record_id",
            "summary_state",
        )
    }


def _assert_frozen_target(target: dict, row: dict) -> None:
    for name in (
        "affair_id",
        "summary_id",
        "process_id",
        "title",
        "body_type",
        "template_id",
        "form_app_id",
        "form_record_id",
    ):
        if str(target.get(name) or "") != str(row.get(name) or ""):
            raise WorkflowRevokeContractMismatch(
                f"The OA workflow {name} changed after revoke authorization."
            )


def _select_exact_sent_row(page, row: dict) -> None:
    try:
        result = page.evaluate(
            _SELECT_SENT_ROW_SCRIPT,
            {"affair_id": row["affair_id"], "subject": row["title"]},
        )
    except Exception as exc:
        raise WorkflowRevokeContractMismatch(
            "The authorized OA sent row could not be selected."
        ) from exc
    if not isinstance(result, dict) or result.get("selected") is not True:
        raise WorkflowRevokeContractMismatch(
            "The authorized OA sent row could not be selected uniquely."
        )


def _open_revoke_dialog(page):
    try:
        control = page.locator("#cancelWorkFlow_a")
        if control.count() != 1 or not control.is_visible():
            raise WorkflowRevokeContractMismatch(
                "The OA revoke control is not uniquely available."
            )
        control.click(timeout=10000)
    except WorkflowRevokeContractMismatch:
        raise
    except Exception as exc:
        raise WorkflowRevokeContractMismatch(
            "The OA revoke dialog could not be opened."
        ) from exc
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for frame in list(page.frames):
            if "method=showRepealCommentDialog" not in str(frame.url or ""):
                continue
            try:
                if frame.locator("#comment").count() == 1:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(100)
    raise WorkflowRevokeContractMismatch(
        "OA did not open the registered revoke-comment dialog."
    )


def _fill_revoke_comment(dialog_frame, comment: str) -> None:
    try:
        control = dialog_frame.locator("#comment")
        if control.count() != 1:
            raise WorkflowRevokeContractMismatch(
                "The OA revoke-comment control is not unique."
            )
        control.fill(comment)
        if str(control.input_value() or "").strip() != comment:
            raise WorkflowRevokeContractMismatch(
                "The OA revoke comment did not match the authorized value."
            )
    except WorkflowRevokeContractMismatch:
        raise
    except Exception as exc:
        raise WorkflowRevokeContractMismatch(
            "The OA revoke comment could not be filled."
        ) from exc


def _confirm_revoke_dialog(page) -> None:
    try:
        result = page.evaluate(_CONFIRM_REVOKE_SCRIPT)
    except Exception as exc:
        raise WorkflowRevokeOutcomeUnknown(
            "The authorized OA revoke confirmation could not be activated."
        ) from exc
    if not isinstance(result, dict) or result.get("clicked") is not True:
        raise WorkflowRevokeOutcomeUnknown(
            "The OA revoke confirmation control was not uniquely identifiable."
        )


def _wait_for_revoke_readback(
    adapter,
    worker,
    *,
    target: dict,
    action_page=None,
    timeout_seconds: float,
) -> dict:
    deadline = time.monotonic() + max(timeout_seconds, 5)
    affair_id = str(target["affair_id"])
    last_observation: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if action_page is not None:
            pump_browser_events(action_page)
        sent_row, _ = _resolve_collection_row(
            adapter,
            worker,
            collection="sent",
            affair_id=affair_id,
            required=False,
        )
        wait_row, _ = _resolve_collection_row(
            adapter,
            worker,
            collection="wait_send",
            affair_id=affair_id,
            required=False,
        )
        last_observation = {
            "sent_present": sent_row is not None,
            "wait_send_present": wait_row is not None,
            "wait_send_state": wait_row.get("state") if wait_row else None,
            "wait_send_sub_state": wait_row.get("sub_state") if wait_row else None,
            "wait_send_sub_state_name": wait_row.get("sub_state_name") if wait_row else "",
        }
        if sent_row is None and wait_row is not None:
            _assert_revoked_wait_send_target(target, wait_row)
            return {
                "state": wait_row.get("state"),
                "sub_state": wait_row.get("sub_state"),
                "sub_state_name": wait_row.get("sub_state_name"),
            }
        time.sleep(0.8)
    raise WorkflowRevokeOutcomeUnknown(
        "The revoked workflow was not confirmed in both OA sent and wait-send collections. "
        f"Last observation: {json.dumps(last_observation, ensure_ascii=False, sort_keys=True)}"
    )


def _assert_revoked_wait_send_target(target: dict, wait_row: dict) -> None:
    for name in ("affair_id", "summary_id", "process_id", "title"):
        if str(target.get(name) or "") != str(wait_row.get(name) or ""):
            raise WorkflowRevokeOutcomeUnknown(
                f"The OA wait-send revoke readback changed the frozen {name}."
            )
    revoked_state = all(
        (
            wait_row.get("state") == 2,
            wait_row.get("sub_state") == 3,
        )
    ) or str(wait_row.get("sub_state_name") or "").strip() == "撤销"
    if not revoked_state:
        raise WorkflowRevokeOutcomeUnknown(
            "The OA wait-send item did not expose the registered revoked state."
        )


def _validate_revoke_plan(plan: dict) -> None:
    if not isinstance(plan, dict) or plan.get("business_intent") != "revoke_sent_workflow":
        raise WorkflowRevokeContractMismatch(
            "The frozen plan is not a workflow revoke plan."
        )
    contract = plan.get("action_contract")
    if not isinstance(contract, dict) or any(
        (
            contract.get("version") != WORKFLOW_REVOKE_CONTRACT_VERSION,
            contract.get("fingerprint") != workflow_revoke_contract_fingerprint(),
        )
    ):
        raise WorkflowRevokeContractMismatch(
            "The workflow revoke contract changed after authorization."
        )
    target = plan.get("target")
    if not isinstance(target, dict):
        raise WorkflowRevokeContractMismatch("The frozen revoke target is missing.")
    _bounded_identifier(target.get("affair_id"), "affair_id")
    _bounded_identifier(target.get("summary_id"), "summary_id")
    _bounded_identifier(target.get("process_id"), "process_id")


@contextmanager
def _readback_worker(worker):
    fork_page = getattr(worker, "fork_page", None)
    if not callable(fork_page):
        yield worker
        return
    with fork_page() as readback_worker:
        yield readback_worker


def _bounded_identifier(value, name: str) -> str:
    if not isinstance(value, str):
        value = str(value or "")
    value = value.strip()
    if not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _bounded_text(value, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        raise ValueError(f"{name} contains unsupported control characters")
    return value


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_LOAD_COLLECTION_ROWS_SCRIPT = r"""
async ({ manager_method, page_size, max_pages }) => {
  if (typeof window.colManager !== 'function'
      || typeof window.CallerResponder !== 'function') {
    throw new Error('OA grid manager contract is missing');
  }
  const manager = new window.colManager();
  if (typeof manager[manager_method] !== 'function') {
    throw new Error('OA collection manager method is missing');
  }
  const callPage = (page) => new Promise((resolve, reject) => {
    const responder = new window.CallerResponder();
    responder.success = resolve;
    responder.error = () => reject(new Error('OA collection manager request failed'));
    manager[manager_method](
      { page, size: page_size },
      { dumpData: 'false' },
      responder,
    );
  });
  const rows = [];
  let page = 1;
  let pages = 1;
  while (page <= pages && page <= max_pages) {
    const payload = await callPage(page);
    if (!payload || typeof payload !== 'object') {
      throw new Error('OA collection manager returned an invalid payload');
    }
    const data = Array.isArray(payload.data)
      ? payload.data
      : (Array.isArray(payload.rows) ? payload.rows : []);
    rows.push(...data);
    const parsedPages = Number(payload.pages || 1);
    pages = Number.isFinite(parsedPages) && parsedPages > 0 ? parsedPages : 1;
    page += 1;
  }
  if (page <= pages) throw new Error('OA collection exceeds the registered page limit');
  return rows;
}
"""


_REVOKE_PRECHECK_SCRIPT = r"""
({ affair_id, summary_id }) => {
  const required = [
    'callBackendMethod',
    'cancelWorkFlow',
    'onBeforeWorkflowOperationValidate',
    'executeWorkflowBeforeEvent',
    'beforeSubmit',
  ];
  const missing = required.filter((name) => typeof window[name] !== 'function');
  if (missing.length) return { ready: false, missing };
  const secret = window.callBackendMethod(
    'secretAjaxManager',
    'checkUserSecretLevel',
    String(summary_id),
  );
  const repeal = window.callBackendMethod(
    'colManager',
    'checkIsCanRepeal',
    { summaryId: String(summary_id) },
  );
  const affair = window.callBackendMethod(
    'portalAffairManager',
    'checkAffairValid',
    String(affair_id),
  );
  return {
    ready: true,
    secret_message: Array.isArray(secret) ? secret.join(' ') : String(secret || ''),
    repeal_message: String(repeal?.msg || ''),
    affair_message: String(affair || ''),
  };
}
"""


_SELECT_SENT_ROW_SCRIPT = r"""
async ({ affair_id, subject }) => {
  if (!window.grid?.grid || typeof window.$ !== 'function') {
    throw new Error('OA sent grid is missing');
  }
  window.$('#listSent').ajaxgridLoad({ subject: String(subject), dumpData: 'false' });
  const deadline = Date.now() + 20000;
  let rows = [];
  while (Date.now() < deadline) {
    if (!window.grid.grid.loading) {
      rows = window.grid.grid.getPageRows();
      if (rows.some((row) => String(row.affairId || '') === String(affair_id))) break;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const indexes = [];
  rows.forEach((row, index) => {
    if (String(row.affairId || '') === String(affair_id)) indexes.push(index);
  });
  if (indexes.length !== 1) return { selected: false, matches: indexes.length };
  const index = indexes[0];
  const checkboxes = Array.from(document.querySelectorAll('input[gridrowcheckbox][row]'));
  for (const checkbox of checkboxes) {
    const shouldSelect = String(checkbox.getAttribute('row')) === String(index);
    if (checkbox.checked !== shouldSelect) checkbox.click();
  }
  const selected = window.grid.grid.getSelectRows();
  return {
    selected: selected.length === 1
      && String(selected[0].affairId || '') === String(affair_id),
    matches: indexes.length,
  };
}
"""


_CONFIRM_REVOKE_SCRIPT = r"""
() => {
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 0
      && rect.height > 0;
  };
  const elements = Array.from(document.querySelectorAll(
    'button, a, [role="button"], input[type="button"], input[type="submit"]',
  ));
  const candidates = elements.filter((element) => {
    const label = String(element.value || element.textContent || '').replace(/\s+/g, ' ').trim();
    return visible(element) && label === '确定';
  });
  if (candidates.length !== 1) {
    return { clicked: false, candidates: candidates.length };
  }
  candidates[0].click();
  return { clicked: true };
}
"""
