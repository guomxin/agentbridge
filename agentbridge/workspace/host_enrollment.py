"""First Workspace enrollment via an administrator's preverified Telegram sender."""

import time

from agentbridge.core.store_transactions import store_transaction

from .nanobot_gateway import pi_telegram_sender, route_specs_for
from .telegram_pilot_links import active_claimants, numeric_id


def _deployment(db, owner, instance, bot, sender, *, new):
    selected = db.execute(
        """SELECT i.route_ref, i.state, i.host_type FROM workspace_host_instances i
           JOIN workspace_host_visibility v USING(instance_id)
           WHERE i.instance_id=? AND v.user_subject=? AND i.host_type IN ('nanobot','pi')
           AND i.all_subjects=0""", (instance, owner),
    ).fetchone()
    route = route_specs_for(selected["host_type"]).get(selected["route_ref"], {}) if selected else {}
    assigned = route.get("enrollmentSenders") or {}
    credential = (route.get("subjects") or {}).get(owner)
    if (not selected or selected["state"] not in (("enabled",) if new else ("enabled", "draining"))
            or route.get("instanceId") != instance or str((route.get("telegram") or {}).get("botId")) != bot
            or selected["host_type"] == "pi" and pi_telegram_sender(route, owner, bot) != sender
            or not credential or assigned.get(owner) != sender or list(assigned.values()).count(sender) != 1):
        raise PermissionError("没有已核验的私聊开通分配。")
    return credential, selected["host_type"]


def confirm_new_user(service, identity, instance, *, bot_id, sender_id, code):
    bot, sender = numeric_id(bot_id), numeric_id(sender_id)
    owner = identity["user_subject"]
    with store_transaction(service.tasks) as db:
        _, host_type = _deployment(db, owner, instance, bot, sender, new=True)
        from .telegram_pair_migration import independent
        if independent(db, owner, instance, bot):
            raise PermissionError("请在已开通的工作台核对本实例的独立配对。")
        if active_claimants(db, sender) - {owner}:
            raise PermissionError("私聊端点已有不同归属。")
        peers = db.execute(
            """SELECT owner,sender FROM workspace_telegram_pilot_links
               WHERE instance=? AND bot=? AND (owner=? OR sender=?)""",
            (instance, bot, owner, sender),
        ).fetchall()
        if any(tuple(row) != (owner, sender) for row in peers):
            raise PermissionError("私聊端点已有不同归属。")
        key = f"enrollment:{instance}:{bot}:{sender}"
        result = service.confirm_workspace_link(
            user_subject=owner, token_id=identity["token_id"], agent_host=host_type,
            endpoint_key=key, client_type="telegram", external_subject=sender, account_id=bot,
            conversation_ref=key, link_code=code, route={"channel":"telegram","to":sender,"accountId":bot},
            connection=db,
        )
        if not peers:
            db.execute("INSERT INTO workspace_telegram_pilot_links VALUES(?,?,?,?,?)",
                       (instance, bot, owner, sender, time.time()))
        return result


def enrolled_workspace_binding(service, owner, instance, bot_id, sender_id, *, token_id=None, protocol=None):
    """Resolve only a consumed first-login proof and its still-authorized Web binding."""
    bot, sender = numeric_id(bot_id), numeric_id(sender_id)
    with service.workspace._connect() as db:
        db.execute("BEGIN")
        credential, host_type = _deployment(db, owner, instance, bot, sender, new=False)
        from .telegram_pair_migration import permits_delivery
        if not permits_delivery(db, host_type, owner, instance, f"telegram:{bot}:{sender}", token_id, protocol):
            raise PermissionError("当前独立配对未授权使用该开通记录。")
        account = db.execute(
            """SELECT a.account_id FROM workspace_accounts a
               JOIN workspace_link_challenges l ON a.user_subject=l.user_subject
               JOIN client_endpoints e ON e.endpoint_id=l.approver_endpoint_id
               WHERE a.state='active' AND a.user_subject=? AND l.state='consumed'
               AND e.user_subject=a.user_subject AND e.agent_host=? AND e.state='active'
               AND e.endpoint_key=? AND e.client_type='telegram' AND e.account_id=? AND e.external_subject=?""",
            (owner, host_type, f"enrollment:{instance}:{bot}:{sender}", bot, sender),
        ).fetchone()
        if not account:
            raise PermissionError("尚无该主体的已完成工作台开户记录。")
        found = service.workspace.hosts.resolve(db, account["account_id"], instance_id=instance, existing=True)
        if found["credential_ref"] != credential:
            raise PermissionError("工作台个人凭据引用已变更。")
        return {key: found[key] for key in
                ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")}
