"""Provision Workspace instances from deployment-controlled route references."""

from __future__ import annotations

from datetime import datetime, timezone

from agentbridge.core.store_transactions import store_transaction
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.host_instances import (
    HostInstanceConflict, LEGACY_INSTANCE_ID, WorkspaceHostInstances, _alias, _label,
)
from agentbridge.workspace.nanobot_gateway import configured_adapters, pi_telegram_sender, route_subjects


class WorkspaceHostProvision:
    def __init__(self, service, audit, workspace_gateway):
        self.service = service
        self.directory = WorkspaceHostInstances(service.db_path)
        self.audit = audit
        self.gateway = workspace_gateway

    def _routes(self):
        routes = {LEGACY_INSTANCE_ID: {"host_type": "openclaw", "instance_id": LEGACY_INSTANCE_ID,
                                      "adapter_version": "legacy", "subjects": {}, "client": self.gateway}}
        try:
            for alias, spec, client in configured_adapters():
                _alias(alias, "route")
                instance = _alias(spec["instanceId"], "instance")
                credentials = route_subjects(spec)
                routes[alias] = {"host_type": client.host_type, "instance_id": instance,
                                 "adapter_version": _label(spec["adapterVersion"], "version", 80),
                                 "subjects": credentials, "client": client}
                if client.host_type == "pi":
                    routes[alias]["telegram_subjects"] = [subject for subject in credentials if pi_telegram_sender(spec, subject)]
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ValueError("部署的宿主连接清单无效，请核对受控配置。") from exc
        return routes

    def catalog(self):
        route_error = False
        try:
            routes = self._routes()
        except ValueError:
            # The operator can always revoke an existing entry, including when the deployment file is invalid.
            routes, route_error = {}, True
        with self.service.workspace._connect() as db:
            accounts = [dict(row) for row in db.execute(
                "SELECT account_id, user_subject, username FROM workspace_accounts WHERE state='active' "
                "ORDER BY username")]
            bindings = [dict(row) for row in db.execute(
                "SELECT b.instance_id, a.user_subject, b.endpoint_id, b.endpoint_key, b.session_key, b.credential_ref, "
                "r.accepted_level, r.compatibility_status "
                "FROM workspace_host_bindings b JOIN workspace_accounts a ON a.account_id=b.account_id "
                "JOIN workspace_host_instances h USING(instance_id) "
                "LEFT JOIN agent_host_registrations r ON r.user_subject=a.user_subject AND r.agent_host=h.host_type "
                "AND r.host_instance_id=b.instance_id AND r.host_version=h.adapter_version "
                "ORDER BY b.instance_id, a.user_subject")]
        public_routes = [{"route_ref": alias, **{k: spec[k] for k in ("instance_id", "adapter_version", "host_type", "telegram_subjects") if k in spec},
                          "subjects": sorted(spec["subjects"])} for alias, spec in routes.items()]
        return {"items": self.directory.admin_list(), "routes": public_routes, "route_error": route_error,
                "accounts": accounts, "bindings": bindings}

    def save(self, actor, request_ip, body):
        required = {"instance_id", "display_name", "route_ref", "state", "subjects", "expected_revision", "reason"}
        if set(body) != required:
            raise ValueError("实例配置字段无效。")
        instance_id = _alias(body["instance_id"], "instance")
        route_ref = _alias(body["route_ref"], "route")
        with store_transaction(self.service.tasks) as db:
            current = db.execute("SELECT * FROM workspace_host_instances WHERE instance_id=?",
                                 (instance_id,)).fetchone()
            if current and current["route_ref"] == route_ref and body["state"] in ("draining", "revoked"):
                host_type, version = current["host_type"], current["adapter_version"]
            else:
                route = self._routes().get(route_ref)
                if not route or route["instance_id"] != instance_id:
                    raise ValueError("该实例没有匹配的受控连接。")
                host_type, version = route["host_type"], route["adapter_version"]
            result = self.directory.save_instance(
                instance_id, display_name=body["display_name"], route_ref=route_ref, host_type=host_type,
                adapter_version=version, state=body["state"], subjects=body["subjects"],
                expected_revision=body["expected_revision"], actor=actor["username"], reason=body["reason"],
                connection=db,
            )
            self.audit.append(actor=actor, action="workspace.host.configure", result="succeeded",
                              target_type="workspace_host", target_id=instance_id, request_ip=request_ip,
                              reason=body["reason"], after={"state": result["state"], "revision": result["revision"]},
                              connection=db)
        return result

    def bind(self, actor, request_ip, body):
        if set(body) != {"instance_id", "account_id", "expected_revision", "reason"}:
            raise ValueError("主体开通字段无效。")
        instance_id = _alias(body["instance_id"], "instance")
        account_id = _label(body["account_id"], "workspace account", 256)
        reason = _label(body["reason"], "reason", 300)
        with store_transaction(self.service.tasks) as db:
            account = self.directory._account(db, account_id)
            instance = next((r for r in self.directory._visible(db, account)
                             if r["instance_id"] == instance_id and r["state"] == "enabled"), None)
            if not instance or instance_id == LEGACY_INSTANCE_ID:
                raise PermissionError("该账号没有可开通的实例。")
            if type(body["expected_revision"]) is not int or instance["revision"] != body["expected_revision"]:
                raise HostInstanceConflict("实例配置已更新，请刷新后确认。")
            route = self._routes().get(instance["route_ref"])
            if not route or any(route[key] != instance[key] for key in ("instance_id", "host_type", "adapter_version")):
                raise ValueError("未找到匹配该实例的受控连接。")
            subject = account["user_subject"]
            credential_ref = route["subjects"].get(subject)
            if credential_ref is None:
                raise PermissionError("部署配置尚未为该主体绑定个人凭据引用。")
            key = f"workspace:{instance_id}:{account_id}"
            session = f"workspace:{instance_id}:{account_id}"
            # A retry must not reactivate or rewrite a revoked or reassigned endpoint.
            endpoint = db.execute("SELECT endpoint_id FROM client_endpoints WHERE agent_host=? AND endpoint_key=?",
                                  (instance["host_type"], key)).fetchone()
            if endpoint:
                endpoint = self.directory._endpoint(db, account, instance, endpoint["endpoint_id"])
                if endpoint is None or endpoint["conversation_ref"] != session:
                    raise PermissionError("已登记端点已失效或与主体不匹配。")
            else:
                endpoint, _ = self.service.tasks.ensure_endpoint(
                    user_subject=subject, token_id=f"workspace-account:{account_id}", agent_host=instance["host_type"],
                    endpoint_key=key, client_type="web", account_id=account_id, external_subject=account_id,
                    conversation_ref=session, label=f"Agent Workspace: {account['username']}",
                    capabilities=["workspace.chat","workspace.task.read"], connection=db,
                )
            self.directory.bind(account_id, instance_id, endpoint_id=endpoint["endpoint_id"],
                                credential_ref=credential_ref, connection=db)
            result = {"instance_id": instance_id, "user_subject": subject, "endpoint_id": endpoint["endpoint_id"],
                      "endpoint_key": key, "session_key": session, "credential_ref": credential_ref}
            self.audit.append(actor=actor, action="workspace.host.bind", result="succeeded",
                              target_type="workspace_host", target_id=instance_id, request_ip=request_ip, reason=reason,
                              after={"user_subject": subject, "endpoint_id": endpoint["endpoint_id"]}, connection=db)
        return result

    def bind_enrolled_account(self, account: dict) -> None:
        """Materialize only the per-subject assignments already approved by an administrator."""
        subject = account["user_subject"]
        routes = self._routes()
        for instance in self.directory.admin_list():
            if instance["instance_id"] == LEGACY_INSTANCE_ID or instance["state"] != "enabled":
                continue
            if instance["subjects"] is not None and subject not in instance["subjects"]:
                continue
            route = routes.get(instance["route_ref"])
            if not route or subject not in route["subjects"]:
                continue
            # A later login may retry after interrupted enrollment. Never repair
            # or re-activate an existing binding as part of a user login.
            with store_transaction(self.service.tasks) as db:
                present = db.execute("SELECT 1 FROM workspace_host_bindings WHERE account_id=? AND instance_id=?",
                                     (account["account_id"], instance["instance_id"])).fetchone()
            if not present:
                self.bind(None, "", {"instance_id": instance["instance_id"], "account_id": account["account_id"],
                          "expected_revision": instance["revision"], "reason": "已确认主体完成工作台账号登记"})

    def probe(self, body):
        if set(body) != {"instance_id"}:
            raise ValueError("连接检查字段无效。")
        instance_id = _alias(body["instance_id"], "instance")
        row = next((r for r in self.directory.admin_list() if r["instance_id"] == instance_id), None)
        route = self._routes().get(row["route_ref"]) if row else None
        if not route or any(route[key] != row[key] for key in ("instance_id", "host_type", "adapter_version")) or not route["client"]:
            result = {"available": False, "code": "HOST_ROUTE_UNAVAILABLE"}
        elif instance_id != LEGACY_INSTANCE_ID:
            result = route["client"].readiness()
        else:
            try:
                route["client"].call("system.info", {}, timeout_seconds=5)
                result = {"available": True, "code": None}
            except GatewayRequestError as exc:
                result = {"available": False, "code": exc.code}
        return {"available": result["available"], "code": result["code"],
                "checked_at": datetime.now(timezone.utc).isoformat(),
                **{key: result[key] for key in ("adapterVersion", "sdkVersion", "acceptedLevel", "capabilities", "modelConfiguration",
                                                 "modelInvocation", "centralConnection", "configuredBindings",
                                                 "verifiedBindings") if key in result}}
