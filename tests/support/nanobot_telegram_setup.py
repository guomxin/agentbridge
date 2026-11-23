"""Create isolated peer Workspace and existing private Telegram identities for the pilot."""

from tests.test_workspace import PASSWORD

INSTANCE = "nanobot-transport-acceptance"


def seed_telegram_peers(fixture):
    service = fixture.service
    directory = service.workspace.hosts
    directory.save_instance(
        INSTANCE,
        host_type="nanobot",
        display_name="Synthetic Telegram pilot",
        route_ref=INSTANCE,
        adapter_version="0.2.0",
        state="enabled",
        subjects=list(fixture.subjects.values()),
        actor="local-fixture",
        reason="isolated Telegram validation",
        expected_revision=0,
    )
    endpoints = {}
    for (label, subject), sender in zip(
        fixture.subjects.items(), ("501", "502"), strict=True
    ):
        link = service.workspace.start_link()
        original = f"test-old-private-{sender}"
        service.confirm_workspace_link(
            user_subject=subject,
            token_id=fixture.tokens[label],
            agent_host="openclaw",
            endpoint_key=original,
            client_type="telegram",
            external_subject=sender,
            conversation_ref=f"agent:main:telegram:direct:{sender}",
            link_code=link["link_code"],
            route={"channel": "telegram", "to": sender, "accountId": "default"},
        )
        account = service.workspace.create_account(
            enrollment_token=link["enrollment_token"],
            username=f"tg-fixture-{label}",
            password=PASSWORD,
        )
        account_id = account["account_id"]
        service.register_workspace_endpoint(account_id=account_id)
        web, _ = service.tasks.ensure_endpoint(
            user_subject=subject,
            token_id=fixture.tokens[label],
            agent_host="nanobot",
            endpoint_key=f"web-fixture-{label}",
            client_type="web",
            external_subject=account_id,
            account_id=account_id,
            conversation_ref=f"tg-fixture-web-{label}",
        )
        directory.bind(
            account_id,
            INSTANCE,
            endpoint_id=web["endpoint_id"],
            credential_ref=f"tg-fixture-web-cred-{label}",
        )
        endpoints[label] = service.tasks.endpoint_for_key(
            user_subject=subject, agent_host="openclaw", endpoint_key=original
        )["endpoint_id"]
    return endpoints
