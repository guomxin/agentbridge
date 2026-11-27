"""H14: failed delivery of a real Pi task notification must not change the verified business effect."""

import time
from datetime import datetime, timezone
from uuid import uuid4

from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks


def pair_before_run(central, post, web):
    service, subject = central.service, web["user_subject"]
    with service.workspace._connect() as db:
        account = db.execute(
            "SELECT account_id FROM workspace_host_bindings WHERE endpoint_id=?", (web["endpoint_id"],)
        ).fetchone()[0]
    links = TelegramPilotLinks(service)
    args = (account, "pi-office", "8801")
    challenge = links.begin(*args)
    confirmation = post("telegram-present", botId="8801", senderId="501", code=challenge["code"]).json()[
        "confirmationCode"
    ]
    assert confirmation and links.confirm(*args, challenge["challenge_id"], confirmation)["state"] == "linked"
    endpoint = post("telegram-route", botId="8801", senderId="501").json()["binding"]
    assert endpoint["user_subject"] == subject and endpoint["endpoint_key"] == "telegram:8801:501"
    # Telegram dates have whole-second precision. A newly paired intake must
    # actually occur after this second, including in synthetic SDK probes.
    paired_second = int(time.time())
    deadline = time.monotonic() + 2
    while int(time.time()) <= paired_second:
        assert time.monotonic() < deadline, "The fixture clock did not advance after pairing"
        time.sleep(.01)


def telegram_request(post, binding, *, message_id=None):
    """Record a fresh synthetic private message through the real intake route."""
    message_id = message_id or (uuid4().int % 1_000_000_000 + 1)
    _, bot, sender = binding["endpoint_key"].split(":")
    stamp = datetime.fromtimestamp(int(time.time()), timezone.utc).isoformat()
    response = post("telegram-received-source", binding=binding, messageId=message_id, receivedAt=stamp)
    assert response.status_code == 200 and response.json().get("accepted") is True, response.status_code
    return {"binding": binding, "requestKey": f"tg:{bot}:{sender}:{message_id}"}


def check_failed_delivery(central, web):
    service, subject = central.service, web["user_subject"]
    route = dict(
        user_subject=subject, agent_host="pi", endpoint_key="telegram:8801:501", host_instance_id="pi-office"
    )

    def business():
        with service.tasks._connect() as db:
            return [
                tuple(row)
                for row in db.execute(
                    """SELECT t.task_id,t.status,o.operation_id,o.status,o.finished_at
                FROM agent_tasks t JOIN task_operations l ON l.task_id=t.task_id AND l.user_subject=t.user_subject
                JOIN operations o ON o.operation_id=l.operation_id AND o.user_subject=l.user_subject
                WHERE t.user_subject=? AND t.agent_host='pi' AND t.origin_endpoint_id=? AND o.status='succeeded'
                ORDER BY o.operation_id""",
                    (subject, web["endpoint_id"]),
                )
            ]

    confirmed = business()
    assert confirmed and all(row[1] == "succeeded" for row in confirmed), [
        (row[1], row[3]) for row in confirmed
    ]
    ids = {row[0] for row in confirmed}
    delivery_id = None
    claim_wait = time.monotonic() + 20
    for attempt in range(1, 6):
        while True:
            batch = service.claim_host_notifications(**route, limit=100, include_workspace_chat=True)[
                "notifications"
            ]
            target = next(
                (
                    item
                    for item in batch
                    if (item.get("task") or {}).get("taskId") in ids
                    and item.get("event", {}).get("eventType")
                    in ("task.completed", "task.operation.succeeded")
                    and (delivery_id is None or item["deliveryId"] == delivery_id)
                ),
                None,
            )
            if target:
                break
            if time.monotonic() > claim_wait:
                raise AssertionError(
                    "Pi companion did not offer the original successful task notification: "
                    + str([(i.get("event", {}).get("eventType"), i.get("deliveryMode")) for i in batch])
                )
            time.sleep(0.12)
        delivery_id = target["deliveryId"]
        assert target["attemptCount"] == attempt
        assert service.validate_host_notification(**route, delivery_id=delivery_id, attempt_count=attempt)[
            "allowed"
        ]
        service.acknowledge_host_notification(
            **route, delivery_id=delivery_id, attempt_count=attempt, succeeded=False, retry_after_seconds=1
        )
        assert business() == confirmed and len(central.matrix.matrix["effects"]) == 1
    failed = service.tasks.get_delivery(delivery_id)
    assert failed["state"] == "failed" and failed["attempt_count"] == 5, failed["state"]
    assert not any(
        n["deliveryId"] == delivery_id
        for n in service.claim_host_notifications(**route, limit=100, include_workspace_chat=True)[
            "notifications"
        ]
    )
    return {
        "notificationOutcome": "failed",
        "notificationAttempts": 5,
        "originalTaskStillSucceeded": True,
        "originalOperationStillSucceeded": True,
        "businessExecutionCount": 1,
    }
