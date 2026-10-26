"""Account-scoped credential diagnostics; never grants or executes access."""
from datetime import datetime, timezone
import json


def credential_health(service, subject, *, now=None):
    now = now or datetime.now(timezone.utc)
    with service.tasks._connect() as db:
        # Some standalone/test deployments do not enable MCP identities.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_identity_tokens'").fetchone():
            return {"state": "unknown", "blocked": False}
        rows = db.execute(
            "SELECT t.state,t.expires_at FROM agent_host_registrations r "
            "LEFT JOIN mcp_identity_tokens t ON t.token_id=r.token_id AND t.user_subject=r.user_subject "
            "WHERE r.user_subject=? AND r.agent_host='openclaw'", (subject,)).fetchall()
    if not rows:
        return {"state": "unknown", "blocked": False}
    valid, invalid = [], 0
    for row in rows:
        try:
            expiry = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
            if row['state'] != 'active' or expiry <= now:
                invalid += 1
            else:
                valid.append(expiry)
        except (AttributeError, ValueError, TypeError):
            invalid += 1
    if not valid:
        return {"state":"unavailable", "blocked":True,
                "message":"当前账号的智能体连接凭证已过期、撤销或不可用。请联系管理员续期或更新凭证；OA 查询尚不能继续。"}
    expiry = min(valid)
    remaining = (expiry-now).total_seconds()
    state = 'partial' if invalid else 'expiring' if remaining <= 7*86400 else 'valid'
    result = {"state":state,"blocked":False,"expiresAt":expiry.isoformat()}
    if state == 'partial':
        result['message'] = '当前账号部分智能体连接凭证不可用，请联系管理员核对；其他主机在线不代表本次连接可用。'
    elif state == 'expiring':
        result['message'] = ('智能体连接凭证将在24小时内到期' if remaining <= 86400 else '智能体连接凭证将在7天内到期') + '，请联系管理员提前续期，避免查询和办理中断。'
    return result


def preparation_health(service, task, *, now=None):
    command = (task.get('summary') or {}).get('command') or {}
    if command.get('state') != 'pending' or task.get('status') not in {'active','running','waiting_user'}:
        return None
    if task.get('current_operation_id') or task.get('current_interaction_id'):
        return None
    with service.tasks._connect() as db:
        if db.execute('SELECT 1 FROM operations WHERE user_subject=? AND idempotency_key=? LIMIT 1',
                      (task['user_subject'],f"workspace-prepare:{task['task_id']}:{command.get('commandKey')}")).fetchone():
            return None  # A real operation supersedes pre-execution diagnostics.
    now = now or datetime.now(timezone.utc)
    try:
        age = max(0, (now-datetime.fromisoformat(command['requestedAt'].replace('Z','+00:00'))).total_seconds())
    except (KeyError, ValueError, TypeError):
        age = 0
    if age >= 600 and command.get('kind') == 'oa_pending_query':
        return {'state':'expired', 'label':'查询等待已超期',
                'message':'本次查询等待已超过10分钟，不能再执行。请重新发送“查询 OA 待办”获取最新结果。'}
    health = credential_health(service, task['user_subject'], now=now)
    if health['blocked']:
        return {'state':'authentication_required','label':'连接凭证不可用','message':health['message'] + ' 原请求已保留，有效期内恢复后会自动接续；请勿重复提交。'}
    with service.tasks._connect() as db:
        row = db.execute("SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
                         "AND event_type='task.prepare.delivery' AND causation_ref=? ORDER BY rowid DESC LIMIT 1",
                         (task['task_id'],task['user_subject'],command.get('commandKey'))).fetchone()
    code = json.loads(row[0]).get('code') if row else None
    if code in {'FORBIDDEN','UNAUTHORIZED','MCP_HTTP_401','HTTP_401'}:
        return {'state':'authentication_required','label':'连接认证失败',
                'message':'智能体拒绝了本次身份绑定，请联系管理员检查连接凭证与账号绑定。原请求已保留，尚未进入业务执行；请勿重复提交。'}
    if (code and code != "accepted") or age >= 30:
        return {'state':'delayed','label':'尚未开始执行',
                'message':'请求已保存，但尚未确认智能体开始执行。请检查连接状态；有效期内系统会尝试接续，无需重复提交。'}
    return None


def record_delivery(service, account, command, code):
    # Diagnostic event only: never change frozen intent, lease or business state.
    with service.tasks._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task = db.execute('SELECT summary_json,status FROM agent_tasks WHERE task_id=? AND user_subject=?',
                          (command['taskId'],account['user_subject'])).fetchone()
        if not task or task['status'] not in {'active','running','waiting_user'}:
            return
        current = json.loads(task['summary_json']).get('command') or {}
        if current.get('commandKey') != command['commandKey'] or current.get('state') != 'pending':
            return
        service.tasks._append_event(db, task_id=command['taskId'], user_subject=account['user_subject'],
            event_type='task.prepare.delivery',payload={'code':code},causation_ref=command['commandKey'],
            created_at=datetime.now(timezone.utc).isoformat())
