"""Pin a skill job to its creator's Workspace host before saving its material."""

from agentbridge.core.endpoint_instances import endpoint_instances

from .host_instances import LEGACY_INSTANCE_ID
from .telegram_pilot_links import mirror_binding, mirror_pair_proof


def _telegram_route(workspace, db, account_id, owner, origin):
    instance, token_id, source = (
        origin["instance"],
        origin["token_id"],
        origin["source"],
    )
    if not isinstance(source, dict) or set(source) != {
        "endpointId",
        "endpointKey",
        "conversationRef",
        "pairingRef",
    }:
        raise PermissionError("缺少原 Pi 私聊评测端点。")
    if any(not isinstance(v, str) or not 0 < len(v) <= 512 for v in source.values()):
        raise PermissionError("原 Pi 私聊标识无效。")
    scope = endpoint_instances(db, owner)
    if scope.get(source["endpointId"]) != ("pi", instance):
        raise PermissionError("当前私聊不属于该主体或 Pi 实例。")
    web = mirror_binding(db, owner, instance, scope)
    if not web or web["host_type"] != "pi" or web["account_id"] != account_id:
        raise PermissionError("此账号未获配原 Pi 网页实例。")
    peer = db.execute(
        """SELECT e.external_subject FROM client_endpoints e JOIN mcp_identity_tokens t ON t.token_id=e.token_id
        WHERE e.endpoint_id=? AND e.user_subject=? AND e.agent_host='pi' AND e.client_type='telegram'
        AND e.state='active' AND e.endpoint_key=? AND e.conversation_ref=? AND e.token_id=?
        AND t.user_subject=e.user_subject AND t.label=? AND t.state='active' AND julianday(t.expires_at)>julianday('now')""",
        (
            source["endpointId"],
            owner,
            source["endpointKey"],
            source["conversationRef"],
            token_id,
            f"workspace-pi:{instance}:{web['credential_ref']}",
        ),
    ).fetchone()
    if not peer or mirror_pair_proof(db, owner, web, peer[0]) != source["pairingRef"]:
        raise PermissionError("原 Pi 私聊与网页的配对、凭据或授权已变更。")
    binding = workspace.hosts.resolve(
        db, account_id, existing=True, instance_id=instance
    )
    return {**binding, "telegram_origin": origin}


def snapshot(workspace, db, owner, *, source_dispatch=None, telegram_origin=None):
    account = db.execute(
        "SELECT account_id FROM workspace_accounts WHERE user_subject=?", (owner,)
    ).fetchone()
    if account is None:
        if source_dispatch or telegram_origin is not None:
            raise PermissionError("无法核验原技能材料所属账号。")
        # Existing non-Workspace authors use the original tool-free gateway.
        return None
    if telegram_origin is not None:
        return _telegram_route(workspace, db, account[0], owner, telegram_origin)
    if source_dispatch:
        original = db.execute(
            "SELECT * FROM agent_host_dispatches WHERE dispatch_id=? AND account_id=? AND user_subject=?",
            (source_dispatch, account[0], owner),
        ).fetchone()
        if not original:
            raise PermissionError("无法核验原技能材料的宿主。")
        # The queue owns a write transaction. Its originating dispatch is already
        # committed and cannot be rebound while this original-task check runs.
        return workspace.dispatch_binding(dict(original))
    return workspace.hosts.resolve(db, account[0])


def checked_binding(workspace, owner, recorded):
    if recorded is not None and (
        not isinstance(recorded, dict)
        or recorded.get("user_subject") != owner
        or not recorded.get("instance_id")
    ):
        raise PermissionError("后台任务的宿主身份不匹配。")
    with workspace._connect() as db:
        db.execute("BEGIN")
        account = db.execute(
            "SELECT account_id FROM workspace_accounts WHERE user_subject=?", (owner,)
        ).fetchone()
        if account is None and recorded is None:
            return None
        if account is None:
            raise PermissionError("后台任务的原工作台账号不可用。")
        if recorded and "telegram_origin" in recorded:
            current = _telegram_route(
                workspace, db, account[0], owner, recorded["telegram_origin"]
            )
        else:
            current = workspace.hosts.resolve(
                db,
                account[0],
                existing=True,
                instance_id=recorded["instance_id"] if recorded else LEGACY_INSTANCE_ID,
            )
    if recorded is not None and current != recorded:
        raise PermissionError("后台任务的原宿主绑定已变更。")
    current.pop("telegram_origin", None)
    return current
