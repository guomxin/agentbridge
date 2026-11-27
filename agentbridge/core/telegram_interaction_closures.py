"""Close an actually delivered companion card without editing or replaying it.

Only new authoritative observations are captured. There is no historical scan:
an acknowledgement can release an already captured observation, never invent one.
"""
import json

from . import actionable_results as actions


STATES = {"completed", "failed", "expired", "superseded", "declined"}
EVENTS = {"completed": "task.interaction.completed", "failed": "task.interaction.failed",
          "expired": "task.interaction.expired", "superseded": "task.interaction.superseded",
          "declined": "task.canceled"}
LABELS = {"credential": "登录卡", "business_input": "填写卡", "execution_authorization": "授权卡"}
SCOPE = ("subject", "host", "instance", "source", "endpoint_id", "endpoint_key", "token_id",
         "pair_proof", "endpoint_hash", "task_id")


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_interaction_card_watches(
        card_delivery_id TEXT PRIMARY KEY, armed_at TEXT NOT NULL, checked_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_interaction_closures(
        delivery_id TEXT PRIMARY KEY, card_delivery_id TEXT NOT NULL,
        interaction_id TEXT NOT NULL, interaction_type TEXT NOT NULL, closed_state TEXT NOT NULL,
        UNIQUE(card_delivery_id,closed_state))""")


def text(kind, state):
    label = LABELS[kind]
    if state == "completed":
        return {"credential": "此前发送的登录卡已完成登录，无需再次输入。请以原任务的当前状态和结果为准。",
                "business_input": "此前发送的填写卡已接收内容，无需再次填写。请以原任务结果为准。",
                "execution_authorization": "此前发送的授权卡已接收授权，无需再次授权。业务结果仍以原任务核验为准。"}[kind]
    return {"failed": f"此前发送的{label}未能完成，旧卡已关闭。请回到原任务查看状态。",
            "expired": f"此前发送的{label}已过期，请勿继续使用旧卡。请回到原任务查看状态。",
            "superseded": f"此前发送的{label}已被替换，请勿继续使用旧卡。请回到原任务查看最新入口。",
            "declined": f"此前发送的{label}已被拒绝，旧卡已关闭。请以原任务当前状态为准。"}[state]


def observation(event):
    payload = event.get("payload") or {}
    state, kind, interaction = payload.get("state"), payload.get("interactionType"), payload.get("interactionId")
    if (state in STATES and kind in LABELS and isinstance(interaction, str)
            and event.get("eventType") == EVENTS[state]):
        return interaction, kind, state
    return None


def original(db, *, subject, task, interaction, endpoint=None):
    # Only a positively receipted send, or its still-unsettled unique begin,
    # can own a close. Unknown sends can only wait for an exact late receipt;
    # they never acquire another send permit. Unissued/legacy ACKs cannot qualify.
    return db.execute("""SELECT d.* FROM telegram_notification_deliveries d
        JOIN telegram_interaction_card_watches w ON w.card_delivery_id=d.delivery_id
        WHERE subject=? AND task_id=? AND interaction_id=? AND kind='trusted_interaction'
          AND protocol='host_notification_v2' AND state IN ('sent','sending','unknown')
          AND (? IS NULL OR endpoint_id=?) ORDER BY d.rowid""",
        (subject, task, interaction, endpoint, endpoint)).fetchall()


def fact_matches(db, *, subject, task, observed):
    interaction, kind, state = observed
    return bool(db.execute("""SELECT 1 FROM interactions i JOIN task_interactions l USING(interaction_id)
        WHERE i.interaction_id=? AND i.user_subject=? AND l.user_subject=i.user_subject
          AND l.task_id=? AND i.interaction_type=? AND l.last_state=?""",
        (interaction, subject, task, kind, state)).fetchone())


def candidate(db, *, subject, task, endpoint, observed, consumer, proof, target, source):
    if not fact_matches(db, subject=subject, task=task, observed=observed):
        return None
    rows = original(db, subject=subject, task=task, interaction=observed[0], endpoint=endpoint)
    expected = (subject, consumer["host"], consumer["instance"], source, endpoint, target["endpoint_key"],
                consumer["token_id"], proof, target["_binding_hash"], task)
    rows = [row for row in rows if tuple(row[k] for k in SCOPE) == expected]
    if len(rows) != 1:
        return None
    row = rows[0]
    if row["state"] == "sent" and (type(row["receipt"]) is not int or row["receipt"] <= 0):
        return None
    return row


def attach(db, delivery, card, observed):
    db.execute("INSERT INTO telegram_interaction_closures VALUES(?,?,?,?,?)",
               (delivery, card["delivery_id"], *observed))


def metadata(db, delivery):
    return db.execute("SELECT * FROM telegram_interaction_closures WHERE delivery_id=?", (delivery,)).fetchone()


def current(db, row, event, closed):
    observed = observation(event)
    if (not observed or observed != (closed["interaction_id"], closed["interaction_type"], closed["closed_state"])
            or not fact_matches(db, subject=row["subject"], task=row["task_id"], observed=observed)
            or row["kind"] != "public_status" or row["interaction_id"] != observed[0]
            or row["public_text"] != text(observed[1], observed[2])):
        return False
    card = db.execute("SELECT * FROM telegram_notification_deliveries WHERE delivery_id=?",
                      (closed["card_delivery_id"],)).fetchone()
    if (not card or any(card[k] != row[k] for k in SCOPE) or card["kind"] != "trusted_interaction"
            or card["protocol"] != row["protocol"] or card["interaction_id"] != observed[0]
            or card["state"] != "sent" or type(card["receipt"]) is not int or card["receipt"] <= 0):
        return False
    source = db.execute("SELECT * FROM task_events WHERE event_id=? AND task_id=? AND user_subject=?",
                        (card["event_id"], row["task_id"], row["subject"])).fetchone()
    if not source or source["event_type"] != "task.interaction.waiting":
        return False
    envelope = {"eventId": source["event_id"], "taskId": source["task_id"],
                "eventType": source["event_type"], "payload": json.loads(source["payload_json"])}
    if "pairGeneration" in event:
        envelope["pairGeneration"] = event["pairGeneration"]
    return (envelope["payload"].get("interactionId") == observed[0]
            and envelope["payload"].get("interactionType") == observed[1]
            and actions._hash(envelope) == card["source_hash"])


def receipt_recorded(db, card):
    # Exact old credentials may settle after revocation. They do not grant a
    # send: claim/issue/begin still verify the frozen current pairing and token.
    db.execute("""UPDATE telegram_notification_deliveries SET state='pending'
        WHERE state='awaiting_card_receipt' AND delivery_id IN
          (SELECT delivery_id FROM telegram_interaction_closures WHERE card_delivery_id=?)
        AND EXISTS (SELECT 1 FROM telegram_notification_deliveries original
          WHERE original.delivery_id=? AND original.state='sent' AND original.receipt>0)""",
        (card["delivery_id"], card["delivery_id"]))


def arm(db, card):
    """Only a new original begin opts into observing this card's lifecycle."""
    now = actions._now().isoformat()
    db.execute("INSERT OR IGNORE INTO telegram_interaction_card_watches VALUES(?,?,?)",
               (card["delivery_id"], now, now))


def observe(service, record, interaction):
    """Observe a governed resource already loaded by the trusted card service."""
    if interaction.get("state") not in STATES:
        return
    subject, ident = record["user_subject"], record["interaction_id"]
    task = service.tasks.task_id_for_interaction(ident, user_subject=subject)
    if not task:
        return
    with service.tasks._connect() as db:
        linked = db.execute("SELECT last_state FROM task_interactions WHERE interaction_id=? AND user_subject=? AND task_id=?",
                            (ident,subject,task)).fetchone()
        if linked and linked[0] == interaction["state"]:
            return
        if not original(db, subject=subject, task=task, interaction=ident):
            return
    service.tasks.link_interaction(task_id=task, user_subject=subject,
                                   interaction_record=record, interaction=interaction)


def poll(service, *, subject, host, instance, endpoint, token, pairing_protocol):
    """Bounded local resource observations, never a model/business/ticket call."""
    from .endpoint_instances import endpoint_instances, instance_key
    from .workspace_query_delivery import pair, registered
    from agentbridge.workspace.telegram_pair_migration import permits_delivery
    from .interactions import InteractionIntegrityError
    with service.tasks._connect() as db:
        if (not registered(db,subject,host,instance_key(host,instance),token)
                or not permits_delivery(db,host,subject,instance,endpoint,token,pairing_protocol)):
            return
        rows = db.execute("""SELECT d.* FROM telegram_notification_deliveries d
            JOIN telegram_interaction_card_watches w ON w.card_delivery_id=d.delivery_id
            WHERE d.subject=? AND d.host=? AND d.instance=? AND d.endpoint_key=? AND d.token_id=?
              AND ((d.state='sent' AND d.receipt>0) OR (d.state IN ('sending','unknown') AND d.expires_at>?))
              AND NOT EXISTS(SELECT 1 FROM telegram_interaction_closures c WHERE c.card_delivery_id=d.delivery_id)
            ORDER BY w.checked_at,d.rowid LIMIT 16""",
            (subject,host,instance_key(host,instance)[1],endpoint,token,actions._now().isoformat())).fetchall()
        ownership = endpoint_instances(db,subject)
        allowed = []
        for row in rows:
            # Failed historical bindings must not occupy the bounded head
            # forever and starve a newly paired original card.
            db.execute("UPDATE telegram_interaction_card_watches SET checked_at=? WHERE card_delivery_id=?",
                       (actions._now().isoformat(),row["delivery_id"]))
            identity = pair(db,subject,row["source"],row["endpoint_id"],ownership)
            if identity and identity[1] == row["pair_proof"] and identity[0]["_binding_hash"] == row["endpoint_hash"]:
                allowed.append(row["interaction_id"])
    for ident in allowed:
        try:
            # _load_interaction checks resource identity and records only this
            # opted-in card's authoritative close. It cannot resume the task.
            service._load_interaction(user_subject=subject,interaction_id=ident)
        except (KeyError,PermissionError,InteractionIntegrityError):
            continue
