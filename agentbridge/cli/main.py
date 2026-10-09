from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlparse

from agentbridge.admin.server import validate_admin_server_config
from agentbridge.admin.stores import AdminAccountStore, AdminAuditStore

from agentbridge.adapters.seeyon_home import (
    parse_navigation_inventory,
    parse_pending_list,
    parse_template_list,
)
from agentbridge.adapters.seeyon_system import SEEYON_OA_URL, build_seeyon_profile
from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.interactive_browser import TrustedInteractiveBrowserApplication
from agentbridge.auth.server import serve_auth_cards, validate_auth_server_config
from agentbridge.broker.credential import CredentialBroker
from agentbridge.broker.remote_browser import (
    RemoteBrowserConfig,
    RemoteInteractiveBrowserBroker,
)
from agentbridge.core.auth_challenges import AuthChallengeStore, ChallengeNotFound
from agentbridge.core.central_service import (
    CentralCapabilityService,
    challenge_response as _challenge_response,
    operation_response as _operation_response,
)
from agentbridge.core.config import ConfigStore, SystemProfile
from agentbridge.core.interactions import InteractionIntegrityError, InteractionNotFound
from agentbridge.core.internal_pki import InternalCertificateAuthorityStore
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.network_security import INSECURE_PRIVATE_HTTP_WARNING
from agentbridge.core.operations import OperationConflictError, OperationStore
from agentbridge.core.runtime_backup import (
    run_runtime_restore_drill,
    validate_backup_manifest,
    validate_runtime_backup,
)
from agentbridge.core.runtime_governance import RuntimeGovernanceStore
from agentbridge.core.home import resolve_home
from agentbridge.core.sessions import SessionPrincipalMismatch, SessionRegistry
from agentbridge.core.tasks import TaskHubStore
from agentbridge.mcp.central import (
    serve_central_mcp,
    validate_central_mcp_server_config,
)
from agentbridge.workspace.gateway import OpenClawGatewayClient
from agentbridge.workspace.server import validate_workspace_server_config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        home = resolve_home(args.home)
    except ValueError as exc:
        print_json(_central_cli_error("HOME_SELECTION_REQUIRED", str(exc)))
        return 2

    if args.area == "admin":
        return handle_admin(args, home)
    if args.area == "system":
        return handle_system(args, ConfigStore(home))
    if args.area == "capability":
        return handle_capability(args, home)
    if args.area == "session":
        return handle_central_session(args, home)
    if args.area == "auth":
        return handle_auth(args, home)
    if args.area == "operation":
        return handle_operation(args, home)
    if args.area == "interaction":
        return handle_interaction(args, home)
    if args.area == "adapter":
        return handle_adapter(args)
    if args.area == "pki":
        return handle_pki(args)
    if args.area == "mcp":
        return handle_mcp(args)
    if args.area == "diagnostics":
        return handle_diagnostics(args, home)
    parser.error("missing command")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbridge")
    parser.add_argument(
        "--home",
        default=None,
        help="AgentBridge state directory (default: ~/.agentbridge; old state requires explicit --home)",
    )
    subparsers = parser.add_subparsers(dest="area", required=True)

    admin = subparsers.add_parser("admin")
    admin_sub = admin.add_subparsers(dest="action", required=True)
    admin_account = admin_sub.add_parser("account")
    admin_account_sub = admin_account.add_subparsers(dest="account_action", required=True)
    admin_bootstrap = admin_account_sub.add_parser("bootstrap")
    admin_bootstrap.add_argument("--username", default="admin")
    admin_bootstrap.add_argument("--password-stdin", action="store_true", required=True)
    system = subparsers.add_parser("system")
    system_sub = system.add_subparsers(dest="action", required=True)
    add = system_sub.add_parser("add")
    add.add_argument("id")
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--origin", action="append", dest="origins")
    status = system_sub.add_parser("status")
    status.add_argument("id")
    system_sub.add_parser("list")
    system_sub.add_parser("init-seeyon-oa")

    capability = subparsers.add_parser("capability")
    capability_sub = capability.add_subparsers(dest="action", required=True)
    capability_list = capability_sub.add_parser("list")
    capability_list.add_argument("--system")
    capability_describe = capability_sub.add_parser("describe")
    capability_describe.add_argument("name")
    capability_invoke = capability_sub.add_parser("invoke")
    capability_invoke.add_argument("name")
    capability_invoke.add_argument("--user-subject", required=True)
    capability_invoke.add_argument("--json", default="{}")
    capability_invoke.add_argument("--idempotency-key")
    capability_invoke.add_argument("--request-id")
    capability_invoke.add_argument("--base-url")
    capability_invoke.add_argument("--taihua-base-url")
    capability_invoke.add_argument("--smartlight-base-url")
    capability_invoke.add_argument("--smartlight-allow-insecure-http", action="store_true")
    capability_invoke.add_argument("--yuque-base-url")
    capability_invoke.add_argument("--yuque-organization-id", type=int)
    capability_invoke.add_argument("--card-base-url", default="http://127.0.0.1:8780")

    session = subparsers.add_parser("session")
    session_sub = session.add_subparsers(dest="action", required=True)
    session_status = session_sub.add_parser("status")
    session_status.add_argument("--system", required=True)
    session_status.add_argument("--user-subject", required=True)
    session_status.add_argument("--taihua-base-url")
    session_status.add_argument("--smartlight-base-url")
    session_status.add_argument("--smartlight-allow-insecure-http", action="store_true")
    session_status.add_argument("--yuque-base-url")
    session_status.add_argument("--yuque-organization-id", type=int)
    session_login = session_sub.add_parser("login")
    session_login.add_argument("--system", required=True)
    session_login.add_argument("--user-subject", required=True)
    session_login.add_argument("--expected-principal", required=True)
    session_login.add_argument("--base-url")
    session_login.add_argument("--taihua-base-url")
    session_login.add_argument("--smartlight-base-url")
    session_login.add_argument("--smartlight-allow-insecure-http", action="store_true")
    session_login.add_argument("--yuque-base-url")
    session_login.add_argument("--yuque-organization-id", type=int)
    session_login.add_argument("--card-base-url", default="http://127.0.0.1:8780")
    session_login.add_argument("--challenge-ttl", type=int, default=300)

    auth = subparsers.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_status = auth_sub.add_parser("status")
    auth_status.add_argument("challenge_id")
    auth_serve = auth_sub.add_parser("serve")
    auth_serve.add_argument("--host", default="127.0.0.1")
    auth_serve.add_argument("--port", type=int, default=8780)
    auth_serve.add_argument("--public-base-url")
    auth_serve.add_argument("--tls-cert")
    auth_serve.add_argument("--tls-key")
    auth_serve.add_argument(
        "--allow-insecure-private-http",
        action="store_true",
        help="allow literal private-IP HTTP for a restricted PoC network",
    )
    auth_serve.add_argument("--base-url")
    auth_serve.add_argument("--taihua-base-url")
    auth_serve.add_argument("--smartlight-base-url")
    auth_serve.add_argument("--smartlight-allow-insecure-http", action="store_true")
    auth_serve.add_argument("--yuque-base-url")
    auth_serve.add_argument("--yuque-organization-id", type=int)
    auth_serve.add_argument("--login-timeout", type=float, default=45)

    operation = subparsers.add_parser("operation")
    operation_sub = operation.add_subparsers(dest="action", required=True)
    operation_get = operation_sub.add_parser("get")
    operation_get.add_argument("operation_id")
    operation_list = operation_sub.add_parser("list")
    operation_list.add_argument("--user-subject")
    operation_list.add_argument("--limit", type=int, default=100)

    interaction = subparsers.add_parser("interaction")
    interaction_sub = interaction.add_subparsers(dest="action", required=True)
    interaction_get = interaction_sub.add_parser("get")
    interaction_get.add_argument("interaction_id")
    interaction_get.add_argument("--user-subject", required=True)
    interaction_get.add_argument("--base-url")
    interaction_get.add_argument("--taihua-base-url")
    interaction_get.add_argument("--smartlight-base-url")
    interaction_get.add_argument("--smartlight-allow-insecure-http", action="store_true")
    interaction_get.add_argument("--yuque-base-url")
    interaction_get.add_argument("--yuque-organization-id", type=int)
    interaction_get.add_argument("--card-base-url", default="http://127.0.0.1:8780")
    interaction_resume = interaction_sub.add_parser("resume")
    interaction_resume.add_argument("interaction_id")
    interaction_resume.add_argument("--user-subject", required=True)
    interaction_resume.add_argument("--idempotency-key")
    interaction_resume.add_argument("--base-url")
    interaction_resume.add_argument("--taihua-base-url")
    interaction_resume.add_argument("--smartlight-base-url")
    interaction_resume.add_argument("--smartlight-allow-insecure-http", action="store_true")
    interaction_resume.add_argument("--yuque-base-url")
    interaction_resume.add_argument("--yuque-organization-id", type=int)
    interaction_resume.add_argument(
        "--card-base-url",
        default="http://127.0.0.1:8780",
    )

    adapter = subparsers.add_parser("adapter")
    adapter_sub = adapter.add_subparsers(dest="action", required=True)
    parse_home = adapter_sub.add_parser("parse-seeyon-home")
    parse_home.add_argument(
        "--kind",
        choices=["navigation", "pending", "templates"],
        required=True,
    )
    parse_home.add_argument("--html-file", required=True)
    parse_home.add_argument(
        "--base-url",
        default="http://10.10.50.110/seeyon/main.do?method=main",
    )

    pki = subparsers.add_parser("pki")
    pki_sub = pki.add_subparsers(dest="action", required=True)
    for action in ("init-root", "verify-root", "issue-server"):
        command = pki_sub.add_parser(action)
        command.add_argument("--state-dir", default=str(Path.home() / ".agentbridge" / "pki"),
                             help="Encrypted PKCS#8 CA directory; password prompted locally")
        if action == "init-root":
            command.add_argument("--root-common-name", default="AgentBridge Internal Root CA")
            command.add_argument("--root-valid-days", type=int, default=3650)
        else:
            command.add_argument("--expected-sha256", required=True, help="Reviewed root certificate fingerprint")
        if action == "issue-server":
            command.add_argument("--ip", required=True)
            command.add_argument("--output-dir", required=True)
            command.add_argument("--server-valid-days", type=int, default=397)
            command.add_argument("--force", action="store_true")

    mcp = subparsers.add_parser("mcp")
    mcp_sub = mcp.add_subparsers(dest="action", required=True)
    mcp_central_serve = mcp_sub.add_parser("central-serve")
    mcp_central_serve.add_argument("--host", default="127.0.0.1")
    mcp_central_serve.add_argument("--port", type=int, default=8790)
    mcp_central_serve.add_argument("--public-base-url")
    mcp_central_serve.add_argument("--tls-cert")
    mcp_central_serve.add_argument("--tls-key")
    mcp_central_serve.add_argument("--auth-host", default="127.0.0.1")
    mcp_central_serve.add_argument("--auth-port", type=int, default=8780)
    mcp_central_serve.add_argument("--auth-public-base-url")
    mcp_central_serve.add_argument("--auth-tls-cert")
    mcp_central_serve.add_argument("--auth-tls-key")
    mcp_central_serve.add_argument("--admin-host", default="127.0.0.1")
    mcp_central_serve.add_argument("--admin-port", type=int, default=0)
    mcp_central_serve.add_argument("--admin-public-base-url")
    mcp_central_serve.add_argument("--admin-tls-cert")
    mcp_central_serve.add_argument("--admin-tls-key")
    mcp_central_serve.add_argument("--workspace-host", default="127.0.0.1")
    mcp_central_serve.add_argument("--workspace-port", type=int, default=0)
    mcp_central_serve.add_argument("--workspace-public-base-url")
    mcp_central_serve.add_argument("--workspace-tls-cert")
    mcp_central_serve.add_argument("--workspace-tls-key")
    mcp_central_serve.add_argument("--workspace-gateway-url")
    mcp_central_serve.add_argument("--workspace-gateway-token-file")
    mcp_central_serve.add_argument(
        "--workspace-node-executable",
        default="node",
    )
    mcp_central_serve.add_argument(
        "--allow-insecure-private-http",
        action="store_true",
        help="allow literal private-IP HTTP for a restricted PoC network",
    )
    mcp_central_serve.add_argument("--base-url")
    mcp_central_serve.add_argument("--taihua-base-url")
    mcp_central_serve.add_argument("--smartlight-base-url")
    mcp_central_serve.add_argument(
        "--smartlight-allow-insecure-http",
        action="store_true",
        help="explicitly allow the configured Smartlight downstream to use plain HTTP",
    )
    mcp_central_serve.add_argument("--yuque-base-url")
    mcp_central_serve.add_argument("--yuque-organization-id", type=int)
    mcp_central_serve.add_argument("--login-timeout", type=float, default=45)
    mcp_central_serve.add_argument(
        "--session-keepalive-interval",
        type=float,
        default=0,
        help="seconds between central OA keepalive probes; 0 disables keepalive",
    )
    mcp_central_serve.add_argument(
        "--session-keepalive-lease",
        type=float,
        default=604_800,
        help="maximum seconds to keep OA alive after the latest real user activity",
    )
    mcp_token = mcp_sub.add_parser("token")
    mcp_token_sub = mcp_token.add_subparsers(dest="token_action", required=True)
    mcp_token_issue = mcp_token_sub.add_parser("issue")
    mcp_token_issue.add_argument("--user-subject", required=True)
    mcp_token_issue.add_argument(
        "--expected-principal",
        help="legacy fallback principal for systems without an explicit binding",
    )
    mcp_token_issue.add_argument(
        "--system-principal",
        action="append",
        metavar="SYSTEM=PRINCIPAL",
        help="system-specific downstream principal; may be repeated",
    )
    mcp_token_issue.add_argument("--label")
    mcp_token_issue.add_argument("--ttl-hours", type=int, default=24)
    mcp_token_list = mcp_token_sub.add_parser("list")
    mcp_token_list.add_argument("--user-subject")
    mcp_token_list.add_argument("--limit", type=int, default=100)
    mcp_token_revoke = mcp_token_sub.add_parser("revoke")
    mcp_token_revoke.add_argument("token_id")

    diagnostics = subparsers.add_parser("diagnostics")
    diagnostics_sub = diagnostics.add_subparsers(dest="action", required=True)
    omnichannel = diagnostics_sub.add_parser("omnichannel")
    omnichannel.add_argument(
        "--expect-endpoint",
        action="append",
        default=[],
        metavar="USER_SUBJECT=CLIENT_TYPE",
        help="required active endpoint; may be repeated",
    )
    backup_create = diagnostics_sub.add_parser("backup-create")
    backup_create.add_argument("--output-dir", required=True)
    backup_create.add_argument("--release-id")
    backup_validate = diagnostics_sub.add_parser("backup-validate")
    backup_validate.add_argument("--backup", required=True)
    restore_drill = diagnostics_sub.add_parser("backup-restore-drill")
    restore_drill.add_argument("--manifest", required=True)
    restore_drill.add_argument("--output-dir", required=True)
    observation_start = diagnostics_sub.add_parser("observation-start")
    observation_start.add_argument("--name", required=True)
    observation_start.add_argument("--hours", type=int, default=168)
    observation_start.add_argument("--created-by", default="operator")
    observation_status = diagnostics_sub.add_parser("observation-status")
    observation_status.add_argument("--state")
    observation_status.add_argument("--capture", action="store_true")

    return parser


def handle_diagnostics(args: argparse.Namespace, home: Path) -> int:
    if args.action == "backup-create":
        from agentbridge.core.recovery_bundle import create_recovery_bundle
        report = create_recovery_bundle(
            home,
            Path(args.output_dir),
            release_id=args.release_id or os.environ.get("AGENTBRIDGE_RELEASE_ID") or "development",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.action == "backup-validate":
        path = Path(args.backup)
        report = (
            validate_backup_manifest(path)
            if path.name.endswith(".manifest.json")
            else validate_runtime_backup(path)
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.action == "backup-restore-drill":
        report = run_runtime_restore_drill(
            Path(args.manifest),
            Path(args.output_dir),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.action == "observation-start":
        governance = RuntimeGovernanceStore(
            _central_db_path(home),
            release_id=os.environ.get("AGENTBRIDGE_RELEASE_ID") or "development",
        )
        observation, reused = governance.start_observation(
            name=args.name,
            duration_hours=args.hours,
            created_by=args.created_by,
            policy={
                "mode": "shadow",
                "automaticBusinessRecovery": False,
                "snapshotIntervalMinutes": 60,
            },
        )
        captured = governance.capture_observation_snapshots(force=not reused)
        observation = governance.observation_detail(
            observation["observation_id"]
        )["observation"]
        print(
            json.dumps(
                {"status": "active", "reused": reused, "observation": observation,
                 "capture": captured},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.action == "observation-status":
        governance = RuntimeGovernanceStore(
            _central_db_path(home),
            release_id=os.environ.get("AGENTBRIDGE_RELEASE_ID") or "development",
        )
        captured = (
            governance.capture_observation_snapshots()
            if args.capture
            else None
        )
        print(
            json.dumps(
                {
                    "status": "succeeded",
                    "capture": captured,
                    "items": [
                        governance.observation_detail(item["observation_id"])
                        for item in governance.list_observations(state=args.state)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.action != "omnichannel":
        raise ValueError(f"unknown diagnostics action: {args.action}")
    try:
        report = TaskHubStore.inspect_runtime(_central_db_path(home))
        expectations = []
        missing = []
        user_endpoints = {
            item["user_subject"]: {
                endpoint["client_type"]
                for endpoint in item["endpoints"]
                if endpoint["state"] == "active" and endpoint["count"] > 0
            }
            for item in report["users"]
        }
        for raw in args.expect_endpoint:
            if "=" not in raw:
                raise ValueError(
                    "--expect-endpoint must use USER_SUBJECT=CLIENT_TYPE"
                )
            user_subject, client_type = (
                part.strip() for part in raw.split("=", 1)
            )
            if not user_subject or not client_type:
                raise ValueError(
                    "--expect-endpoint must use USER_SUBJECT=CLIENT_TYPE"
                )
            expectation = {
                "user_subject": user_subject,
                "client_type": client_type,
                "present": client_type in user_endpoints.get(user_subject, set()),
            }
            expectations.append(expectation)
            if not expectation["present"]:
                missing.append(expectation)
        passed = report["isolation"]["passed"] and not missing
        print_json(
            {
                "protocolVersion": "0.1",
                "status": "succeeded" if passed else "failed",
                "expectations": expectations,
                "report": report,
            }
        )
        return 0 if passed else 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        print_json(_central_cli_error("DIAGNOSTICS_FAILED", str(exc)))
        return 2


def handle_admin(args: argparse.Namespace, home: Path) -> int:
    if args.action != "account" or args.account_action != "bootstrap":
        raise ValueError("unknown admin action")
    if not args.password_stdin:
        print_json(_central_cli_error("INVALID_INPUT", "--password-stdin is required"))
        return 2
    password = sys.stdin.readline().rstrip("\r\n")
    try:
        store = AdminAccountStore(_central_db_path(home))
        if store.count() != 0:
            raise ValueError("an administrator account already exists")
        account = store.create(
            username=args.username,
            password=password,
            role="admin",
            must_change_password=True,
        )
        AdminAuditStore(_central_db_path(home)).append(
            actor=account,
            action="admin.account.bootstrap",
            target_type="admin_account",
            target_id=account["account_id"],
            result="succeeded",
            after=account,
        )
    except ValueError as exc:
        print_json(_central_cli_error("ADMIN_BOOTSTRAP_FAILED", str(exc)))
        return 2
    finally:
        password = ""
    print_json(
        {
            "protocolVersion": "0.1",
            "status": "created",
            "account": account,
            "warning": "The bootstrap password must be changed after first sign-in.",
        }
    )
    return 0

def handle_pki(args: argparse.Namespace) -> int:
    import getpass
    import re
    import warnings
    from cryptography.hazmat.primitives import hashes
    from cryptography.exceptions import InvalidSignature
    try:
        # No password flags, environment variables, or output containing passphrases.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            secret = getpass.getpass("CA passphrase: ").encode("utf-8")
            if args.action == "init-root":
                if getpass.getpass("Confirm CA passphrase: ").encode("utf-8") != secret:
                    raise ValueError("CA passphrases do not match")
        store = InternalCertificateAuthorityStore(Path(args.state_dir).expanduser(), secret)
        if args.action == "init-root":
            result = store.create_root(common_name=args.root_common_name, valid_days=args.root_valid_days)
        else:
            certificate, _ = store._load_root()
            fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
            if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_sha256) or fingerprint != args.expected_sha256.lower():
                raise ValueError("Root fingerprint differs from the reviewed CA")
            if args.action == "verify-root":
                result = {"status": "verified", "rootFingerprintSha256": fingerprint}
            elif args.action == "issue-server":
                result = store.issue_server_certificate(server_ip=args.ip, output_dir=Path(args.output_dir).expanduser(),
                    server_valid_days=args.server_valid_days, force=args.force).as_dict()
            else:
                raise ValueError("Unknown PKI action")
    except (OSError, ValueError, TypeError, EOFError, getpass.GetPassWarning, InvalidSignature) as exc:
        print_json(_central_cli_error("PKI_ISSUE_FAILED", str(exc)))
        return 2
    finally:
        secret = b""
    print_json(result)
    return 0


def handle_system(args: argparse.Namespace, store: ConfigStore) -> int:
    if args.action == "add":
        origins = args.origins or [_origin_from_url(args.url)]
        profile = SystemProfile(
            id=args.id,
            name=args.name,
            base_url=args.url,
            allowed_origins=origins,
        )
        store.save_system(profile)
        print_json(asdict(profile))
        return 0
    if args.action == "status":
        print_json(asdict(store.load_system(args.id)))
        return 0
    if args.action == "list":
        print_json([asdict(profile) for profile in store.list_systems()])
        return 0
    if args.action == "init-seeyon-oa":
        profile = build_seeyon_profile()
        store.save_system(profile)
        print_json(asdict(profile))
        return 0
    raise ValueError(f"unknown system action: {args.action}")


def handle_capability(args: argparse.Namespace, home: Path) -> int:
    service = CentralCapabilityService(
        home=home,
        base_url=_central_base_url(home, getattr(args, "base_url", None)),
        taihua_base_url=_taihua_base_url(
            home,
            getattr(args, "taihua_base_url", None),
        ),
        smartlight_base_url=_smartlight_base_url(
            home,
            getattr(args, "smartlight_base_url", None),
        ),
        smartlight_allow_insecure_http=bool(
            getattr(args, "smartlight_allow_insecure_http", False)
        ),
        yuque_base_url=(yuque_url := _yuque_base_url(
            home,
            getattr(args, "yuque_base_url", None),
        )),
        yuque_organization_id=_yuque_organization_id(
            yuque_url,
            getattr(args, "yuque_organization_id", None),
        ),
        trusted_card_base_url=getattr(args, "card_base_url", "http://127.0.0.1:8780"),
    )
    if args.action == "list":
        print_json(service.list_capabilities(system=getattr(args, "system", None)))
        return 0
    if args.action == "describe":
        try:
            response = service.describe_capability(args.name)
        except KeyError as exc:
            print_json(_central_cli_error("CAPABILITY_NOT_FOUND", str(exc)))
            return 2
        print_json(response)
        return 0
    if args.action != "invoke":
        raise ValueError(f"unknown capability action: {args.action}")

    try:
        arguments = json.loads(args.json)
    except json.JSONDecodeError as exc:
        print_json(_central_cli_error("INVALID_INPUT", f"--json is not valid JSON: {exc}"))
        return 2
    if not isinstance(arguments, dict):
        print_json(_central_cli_error("INVALID_INPUT", "--json must decode to an object"))
        return 2

    try:
        response = service.invoke(
            user_subject=args.user_subject,
            capability_name=args.name,
            arguments=arguments,
            idempotency_key=args.idempotency_key,
            request_id=args.request_id,
        )
    except KeyError as exc:
        print_json(_central_cli_error("CAPABILITY_NOT_FOUND", str(exc)))
        return 2
    except (OperationConflictError, ValueError) as exc:
        print_json(_central_cli_error("INVALID_REQUEST", str(exc)))
        return 2
    except PermissionError as exc:
        print_json(_central_cli_error("PERMISSION_DENIED", str(exc)))
        return 2
    print_json(response)
    return 0 if response["status"] in {"succeeded", "requires_user_action"} else 1


def handle_central_session(args: argparse.Namespace, home: Path) -> int:
    service = CentralCapabilityService(
        home=home,
        base_url=_central_base_url(home, getattr(args, "base_url", None)),
        taihua_base_url=_taihua_base_url(
            home,
            getattr(args, "taihua_base_url", None),
        ),
        smartlight_base_url=_smartlight_base_url(
            home,
            getattr(args, "smartlight_base_url", None),
        ),
        smartlight_allow_insecure_http=bool(
            getattr(args, "smartlight_allow_insecure_http", False)
        ),
        yuque_base_url=(yuque_url := _yuque_base_url(
            home,
            getattr(args, "yuque_base_url", None),
        )),
        yuque_organization_id=_yuque_organization_id(
            yuque_url,
            getattr(args, "yuque_organization_id", None),
        ),
    )
    if args.action == "status":
        print_json(service.session_status(user_subject=args.user_subject, system_id=args.system))
        return 0
    if args.action != "login":
        raise ValueError(f"unknown session action: {args.action}")

    response = service.start_login(
        user_subject=args.user_subject,
        expected_principal_ref=args.expected_principal,
        card_base_url=args.card_base_url,
        ttl_seconds=args.challenge_ttl,
        system_id=args.system,
    )
    print_json(response)
    return 0


def handle_auth(args: argparse.Namespace, home: Path) -> int:
    challenge_store = AuthChallengeStore(_central_db_path(home))
    if args.action == "status":
        try:
            challenge = challenge_store.get(args.challenge_id)
        except ChallengeNotFound as exc:
            print_json(_central_cli_error("CHALLENGE_NOT_FOUND", str(exc)))
            return 2
        print_json(
            {
                "protocolVersion": "0.1",
                "status": challenge["state"],
                "challenge": _challenge_response(challenge),
            }
        )
        return 0
    if args.action != "serve":
        raise ValueError(f"unknown auth action: {args.action}")

    try:
        config = validate_auth_server_config(
            host=args.host,
            port=args.port,
            public_base_url=args.public_base_url,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            allow_insecure_private_http=args.allow_insecure_private_http,
        )
    except ValueError as exc:
        print_json(_central_cli_error("AUTH_SERVER_CONFIG_INVALID", str(exc)))
        return 2

    service = CentralCapabilityService(
        home=home,
        base_url=_central_base_url(home, args.base_url),
        taihua_base_url=_taihua_base_url(home, args.taihua_base_url),
        smartlight_base_url=_smartlight_base_url(home, args.smartlight_base_url),
        smartlight_allow_insecure_http=args.smartlight_allow_insecure_http,
        yuque_base_url=(yuque_url := _yuque_base_url(home, args.yuque_base_url)),
        yuque_organization_id=_yuque_organization_id(
            yuque_url, args.yuque_organization_id
        ),
        trusted_card_base_url=config.public_base_url,
    )
    challenge_store = service.challenges
    broker = CredentialBroker(
        challenge_store=challenge_store,
        session_registry=service.sessions,
        session_state_store=service.session_states,
        adapter_factory=lambda challenge: service.adapter_for_system(
            challenge["system_id"]
        ),
        worker_factory=service.authentication_worker,
        login_timeout_seconds=args.login_timeout,
    )
    application = TrustedAuthApplication(challenge_store=challenge_store, broker=broker)
    auth_origin = urlparse(config.public_base_url)
    remote_browser_base_url = (
        f"{auth_origin.scheme}://{auth_origin.hostname}:8781"
    )
    interactive_broker = RemoteInteractiveBrowserBroker(
        challenge_store=challenge_store,
        session_registry=service.sessions,
        session_state_store=service.session_states,
        adapter_factory=lambda challenge: service.adapter_for_system(
            challenge["system_id"]
        ),
        worker_factory=service.remote_authentication_worker,
        config=RemoteBrowserConfig(
            runtime_root=(home / "remote-browser").resolve(),
            public_base_url=remote_browser_base_url,
            listen_host=auth_origin.hostname or config.host,
            listen_port=8781,
            tls_cert=config.tls_cert,
            tls_key=config.tls_key,
            allow_insecure_private_http=config.insecure_private_http,
        ),
        login_timeout_seconds=900,
    )
    interactive_application = TrustedInteractiveBrowserApplication(
        challenge_store=challenge_store,
        broker=interactive_broker,
    )
    action_application = TrustedActionApplication(
        authorization_store=service.write_authorizations
    )
    field_application = TrustedFieldApplication(
        submission_store=service.field_submissions
    )
    startup = {
        "protocolVersion": "0.1",
        "status": "serving",
        "service": "trusted_authentication_card",
        "cardTypes": ["authentication", "business_input", "write_authorization"],
        "listen": {"host": config.host, "port": config.port},
        "publicBaseUrl": config.public_base_url,
        "tls": config.tls_cert is not None,
        "insecurePrivateHttp": config.insecure_private_http,
    }
    if config.insecure_private_http:
        startup["securityWarning"] = INSECURE_PRIVATE_HTTP_WARNING
        print(INSECURE_PRIVATE_HTTP_WARNING, file=sys.stderr, flush=True)
    print_json(startup)
    sys.stdout.flush()
    try:
        serve_auth_cards(
            config=config,
            application=application,
            action_application=action_application,
            field_application=field_application,
            interactive_application=interactive_application,
        )
    except KeyboardInterrupt:
        return 0
    finally:
        interactive_broker.shutdown()
    return 0


def handle_operation(args: argparse.Namespace, home: Path) -> int:
    store = OperationStore(_central_db_path(home))
    if args.action == "get":
        try:
            operation = store.get(args.operation_id)
        except KeyError as exc:
            print_json(_central_cli_error("OPERATION_NOT_FOUND", str(exc)))
            return 2
        print_json({"protocolVersion": "0.1", "operation": _operation_response(operation)})
        return 0
    if args.action == "list":
        operations = store.list(user_subject=args.user_subject, limit=args.limit)
        print_json(
            {
                "protocolVersion": "0.1",
                "count": len(operations),
                "operations": [_operation_response(operation) for operation in operations],
            }
        )
        return 0
    raise ValueError(f"unknown operation action: {args.action}")


def handle_interaction(args: argparse.Namespace, home: Path) -> int:
    service = CentralCapabilityService(
        home=home,
        base_url=_central_base_url(home, args.base_url),
        taihua_base_url=_taihua_base_url(home, args.taihua_base_url),
        smartlight_base_url=_smartlight_base_url(home, args.smartlight_base_url),
        smartlight_allow_insecure_http=args.smartlight_allow_insecure_http,
        yuque_base_url=(yuque_url := _yuque_base_url(home, args.yuque_base_url)),
        yuque_organization_id=_yuque_organization_id(
            yuque_url, args.yuque_organization_id
        ),
        trusted_card_base_url=args.card_base_url,
    )
    try:
        if args.action == "get":
            response = service.get_interaction(
                user_subject=args.user_subject,
                interaction_id=args.interaction_id,
            )
        elif args.action == "resume":
            response = service.resume_interaction(
                user_subject=args.user_subject,
                interaction_id=args.interaction_id,
                idempotency_key=args.idempotency_key,
            )
        else:
            raise ValueError(f"unknown interaction action: {args.action}")
    except InteractionNotFound as exc:
        print_json(_central_cli_error("INTERACTION_NOT_FOUND", str(exc)))
        return 2
    except (InteractionIntegrityError, OperationConflictError, ValueError) as exc:
        print_json(_central_cli_error("INTERACTION_INVALID", str(exc)))
        return 2
    print_json(response)
    return 0 if response.get("status") in {
        None,
        "succeeded",
        "requires_user_action",
        "already_resumed",
    } else 1


def handle_mcp(args: argparse.Namespace) -> int:
    home = Path(args.home)
    if args.action == "token":
        store = McpIdentityTokenStore(_central_db_path(home))
        if args.token_action == "issue":
            if args.ttl_hours < 1 or args.ttl_hours > 24 * 90:
                print_json(
                    _central_cli_error(
                        "INVALID_INPUT",
                        "--ttl-hours must be between 1 and 2160",
                    )
                )
                return 2
            try:
                sessions = SessionRegistry(_central_db_path(home), _central_profile_root(home))
                principal_bindings = _parse_system_principals(args.system_principal or [])
                grant = store.user_grants.get(args.user_subject)
                system_ids = {p.split(".", 1)[0] for p in grant["permissions"]} if grant else set()
                system_ids.update(principal_bindings)
                if args.expected_principal:
                    system_ids.add("oa")
                resolved_bindings = sessions.ensure_principal_bindings(
                    user_subject=args.user_subject,
                    system_ids=system_ids,
                    principal_bindings=principal_bindings,
                    fallback_principal_ref=args.expected_principal,
                )
                token_principal = (
                    str(args.expected_principal or "").strip()
                    or resolved_bindings.get("oa")
                    or (resolved_bindings[sorted(resolved_bindings)[0]] if resolved_bindings else args.user_subject)
                )
                token = store.issue(
                    user_subject=args.user_subject,
                    expected_principal_ref=token_principal,
                    label=args.label,
                    ttl_seconds=args.ttl_hours * 3600,
                )
            except (ValueError, SessionPrincipalMismatch) as exc:
                print_json(_central_cli_error("IDENTITY_BINDING_INVALID", str(exc)))
                return 2
            secret = token.pop("token")
            print_json(
                {
                    "protocolVersion": "0.1",
                    "status": "issued",
                    "identityToken": {
                        **_mcp_identity_response(token),
                        "principalBindings": resolved_bindings,
                    },
                    "bearerToken": secret,
                    "warning": (
                        "The bearer token is shown once. Store it only in the trusted "
                        "MCP client configuration."
                    ),
                }
            )
            return 0
        if args.token_action == "list":
            try:
                tokens = store.list(user_subject=args.user_subject, limit=args.limit)
            except ValueError as exc:
                print_json(_central_cli_error("INVALID_INPUT", str(exc)))
                return 2
            print_json(
                {
                    "protocolVersion": "0.1",
                    "count": len(tokens),
                    "identityTokens": [_mcp_identity_response(token) for token in tokens],
                }
            )
            return 0
        if args.token_action == "revoke":
            try:
                token = store.revoke(args.token_id)
            except KeyError as exc:
                print_json(_central_cli_error("TOKEN_NOT_FOUND", str(exc)))
                return 2
            print_json(
                {
                    "protocolVersion": "0.1",
                    "status": "revoked",
                    "identityToken": _mcp_identity_response(token),
                }
            )
            return 0
        raise ValueError(f"unknown MCP token action: {args.token_action}")

    if args.action != "central-serve":
        raise ValueError(f"unknown mcp action: {args.action}")
    admin_config = None
    workspace_config = None
    try:
        if args.login_timeout < 1 or args.login_timeout > 300:
            raise ValueError("--login-timeout must be between 1 and 300 seconds")
        if args.session_keepalive_interval < 0:
            raise ValueError("--session-keepalive-interval cannot be negative")
        if args.session_keepalive_interval and not (
            60 <= args.session_keepalive_interval <= 1_800
        ):
            raise ValueError(
                "--session-keepalive-interval must be 0 or between 60 and 1800 seconds"
            )
        if not 60 <= args.session_keepalive_lease <= 604_800:
            raise ValueError(
                "--session-keepalive-lease must be between 60 and 604800 seconds"
            )
        if (
            args.session_keepalive_interval
            and args.session_keepalive_lease < args.session_keepalive_interval
        ):
            raise ValueError(
                "--session-keepalive-lease cannot be shorter than the keepalive interval"
            )
        mcp_config = validate_central_mcp_server_config(
            host=args.host,
            port=args.port,
            public_base_url=args.public_base_url,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            allow_insecure_private_http=args.allow_insecure_private_http,
        )
        auth_config = validate_auth_server_config(
            host=args.auth_host,
            port=args.auth_port,
            public_base_url=args.auth_public_base_url,
            tls_cert=args.auth_tls_cert,
            tls_key=args.auth_tls_key,
            allow_insecure_private_http=args.allow_insecure_private_http,
        )
        if mcp_config.port == auth_config.port:
            raise ValueError("central MCP and authentication card services must use different ports")
        if args.admin_port:
            admin_config = validate_admin_server_config(
                host=args.admin_host,
                port=args.admin_port,
                public_base_url=args.admin_public_base_url,
                tls_cert=args.admin_tls_cert,
                tls_key=args.admin_tls_key,
            )
            if admin_config.port in {mcp_config.port, auth_config.port}:
                raise ValueError("admin, MCP, and authentication services must use different ports")
        gateway_configured = bool(args.workspace_gateway_url) or bool(
            args.workspace_gateway_token_file
        )
        if bool(args.workspace_gateway_url) != bool(
            args.workspace_gateway_token_file
        ):
            raise ValueError(
                "workspace Gateway URL and token file must be configured together"
            )
        if gateway_configured and not args.workspace_port:
            raise ValueError(
                "workspace Gateway configuration requires --workspace-port"
            )
        if args.workspace_port:
            workspace_config = validate_workspace_server_config(
                host=args.workspace_host,
                port=args.workspace_port,
                public_base_url=args.workspace_public_base_url,
                tls_cert=args.workspace_tls_cert,
                tls_key=args.workspace_tls_key,
            )
            used_ports = {mcp_config.port, auth_config.port}
            if admin_config is not None:
                used_ports.add(admin_config.port)
            if workspace_config.port in used_ports:
                raise ValueError(
                    "workspace, admin, MCP, and authentication services must use different ports"
                )
    except ValueError as exc:
        print_json(_central_cli_error("CENTRAL_MCP_CONFIG_INVALID", str(exc)))
        return 2
    service = CentralCapabilityService(
        home=home,
        base_url=_central_base_url(home, args.base_url),
        taihua_base_url=_taihua_base_url(home, args.taihua_base_url),
        smartlight_base_url=_smartlight_base_url(home, args.smartlight_base_url),
        smartlight_allow_insecure_http=args.smartlight_allow_insecure_http,
        yuque_base_url=(yuque_url := _yuque_base_url(home, args.yuque_base_url)),
        yuque_organization_id=_yuque_organization_id(
            yuque_url, args.yuque_organization_id
        ),
        trusted_card_base_url=auth_config.public_base_url,
        session_keepalive_lease_seconds=args.session_keepalive_lease,
    )
    identity_store = McpIdentityTokenStore(_central_db_path(home))
    workspace_gateway = None
    if args.workspace_gateway_url and args.workspace_gateway_token_file:
        workspace_gateway = OpenClawGatewayClient(
            url=args.workspace_gateway_url,
            token_file=args.workspace_gateway_token_file,
            state_dir=home / "workspace-gateway",
            node_executable=args.workspace_node_executable,
        )
    insecure_private_http = (
        mcp_config.insecure_private_http or auth_config.insecure_private_http
    )
    startup = {
        "protocolVersion": "0.1",
        "status": "serving",
        "service": "agentbridge_central_mcp",
        "mcpUrl": mcp_config.mcp_url,
        "authCardBaseUrl": auth_config.public_base_url,
        "adminBaseUrl": admin_config.public_base_url if admin_config else None,
        "workspaceBaseUrl": (
            workspace_config.public_base_url if workspace_config else None
        ),
        "workspaceGatewayConfigured": workspace_gateway is not None,
        "transport": "streamable_http",
        "stateless": True,
        "authentication": "bearer_identity_token",
        "insecurePrivateHttp": insecure_private_http,
        "sessionKeepalive": {
            "enabled": args.session_keepalive_interval > 0,
            "intervalSeconds": args.session_keepalive_interval,
            "activityLeaseSeconds": args.session_keepalive_lease,
        },
    }
    if insecure_private_http:
        startup["securityWarning"] = INSECURE_PRIVATE_HTTP_WARNING
        print(INSECURE_PRIVATE_HTTP_WARNING, file=sys.stderr, flush=True)
    print_json(startup)
    sys.stdout.flush()
    try:
        serve_central_mcp(
            service=service,
            identity_store=identity_store,
            mcp_config=mcp_config,
            auth_config=auth_config,
            admin_config=admin_config,
            workspace_config=workspace_config,
            workspace_gateway=workspace_gateway,
            login_timeout_seconds=args.login_timeout,
            keepalive_interval_seconds=args.session_keepalive_interval,
            keepalive_activity_lease_seconds=args.session_keepalive_lease,
        )
    except KeyboardInterrupt:
        return 0
    return 0


def handle_adapter(args: argparse.Namespace) -> int:
    if args.action == "parse-seeyon-home":
        html = Path(args.html_file).read_text(encoding="utf-8")
        if args.kind == "pending":
            print_json(parse_pending_list(html, base_url=args.base_url))
            return 0
        if args.kind == "navigation":
            print_json(parse_navigation_inventory(html, base_url=args.base_url))
            return 0
        if args.kind == "templates":
            print_json(parse_template_list(html, base_url=args.base_url))
            return 0
    raise ValueError(f"unknown adapter action: {args.action}")


def _parse_system_principals(values: list[str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for value in values:
        system_id, separator, principal_ref = str(value or "").partition("=")
        system_id = system_id.strip()
        principal_ref = principal_ref.strip()
        if (
            separator != "="
            or system_id not in {"oa", "taihua", "smartlight", "yuque"}
            or not principal_ref
        ):
            raise ValueError(
                "--system-principal must use oa|taihua|smartlight|yuque=PRINCIPAL"
            )
        if system_id in bindings and bindings[system_id] != principal_ref:
            raise ValueError(f"duplicate principal binding for {system_id}")
        bindings[system_id] = principal_ref
    return bindings


def _mcp_identity_response(token: dict) -> dict:
    return {
        "tokenId": token["token_id"],
        "userSubject": token["user_subject"],
        "expectedPrincipalRef": token["expected_principal_ref"],
        "label": token.get("label"),
        "scopes": token["scopes"],
        "state": token["state"],
        "createdAt": token["created_at"],
        "expiresAt": token["expires_at"],
        "lastUsedAt": token.get("last_used_at"),
        "revokedAt": token.get("revoked_at"),
    }


def _central_db_path(home: Path) -> Path:
    return home / "agentbridge.db"


def _central_profile_root(home: Path) -> Path:
    return home / "profiles"


def _central_session_secret_root(home: Path) -> Path:
    return home / "session-secrets"


def _central_base_url(home: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        return ConfigStore(home).load_system("oa").base_url
    except KeyError:
        return SEEYON_OA_URL


def _taihua_base_url(home: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        return ConfigStore(home).load_system("taihua").base_url
    except KeyError:
        return None


def _smartlight_base_url(home: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        return ConfigStore(home).load_system("smartlight").base_url
    except KeyError:
        return None


def _yuque_base_url(home: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        return ConfigStore(home).load_system("yuque").base_url
    except KeyError:
        return None


def _yuque_organization_id(
    base_url: str | None,
    explicit: int | None,
) -> int | None:
    if base_url is None:
        return None
    organization_id = explicit if explicit is not None else 20020375
    if organization_id <= 0:
        raise ValueError("Yuque organization id must be positive")
    return organization_id


def _central_cli_error(code: str, message: str) -> dict:
    return {
        "protocolVersion": "0.1",
        "status": "failed",
        "error": {"code": code, "message": message},
    }


def print_json(value) -> None:
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(value, ensure_ascii=True, indent=2))


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("url must include scheme and host")
    return f"{parsed.scheme}://{parsed.netloc}"


if __name__ == "__main__":
    raise SystemExit(main())
