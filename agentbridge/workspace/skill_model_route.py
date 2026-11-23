"""Pin a skill job to its creator's Workspace host before saving its material."""

from .host_instances import LEGACY_INSTANCE_ID


def snapshot(workspace, db, owner, *, source_dispatch=None):
    account = db.execute(
        "SELECT account_id FROM workspace_accounts WHERE user_subject=?", (owner,)
    ).fetchone()
    if account is None:
        if source_dispatch:
            raise PermissionError("无法核验原技能材料所属账号。")
        # Existing non-Workspace authors use the original tool-free gateway.
        return None
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
        binding = workspace.hosts.resolve(
            db,
            account[0],
            existing=True,
            instance_id=recorded["instance_id"] if recorded else LEGACY_INSTANCE_ID,
        )
    if recorded is not None and binding != recorded:
        raise PermissionError("后台任务的原宿主绑定已变更。")
    return binding
