"""Independent card navigation/content fixture over real Central/Auth/Workspace.

OA transport and DOM are closed synthetic IO. Expiry uses the field store's
clock to expire one genuine old source, then restores the clock before the
normal Central prepare creates its replacement. No decision or result is seeded.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.quick_queries import queue_query, execute_query
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.full_business_acceptance import OWNER
from tests.support.interaction_acceptance import SyntheticBroker, write_metadata
from tests.support.pending_readiness_acceptance import PendingReadinessAcceptanceFixture
from tests.test_workspace import PASSWORD


RETAINED_OPINION = "合成旧页保留的意见：只恢复文字，须重新核对材料并明确确认。"


class CardContentAcceptanceFixture(PendingReadinessAcceptanceFixture):
    def __init__(self, home):
        super().__init__(home, modes=())
        self.credentials_path = self.home / "browser-credentials.json"
        write_metadata(self.credentials_path, {"username": "cardreview", "password": PASSWORD,
            "otherUsername": "cardother", "otherPassword": PASSWORD})
        self.flow = TrustedFlowApplication(service=self.service,
            field_application=TrustedFieldApplication(submission_store=self.service.field_submissions),
            action_application=TrustedActionApplication(authorization_store=self.service.write_authorizations),
            workspace_base_url=self.workspace_url)
        if self.meta.get("card_content_initialized"):
            return
        if self.meta.get("full_cases"):
            raise ValueError("Use a fresh independent home for card content acceptance")
        self.meta["card_content_selectable"] = []
        for index in range(12):
            profile = ("efficiency_data", "weekly_report", "attendance_confirmation")[index % 3]
            affair = f"synthetic-content-select-{index + 1:02}"
            self.oa.add_affair(affair, profile=profile)
            item = self.oa.data["affairs"][affair]
            item["source"]["title"] += f"（导航验收 {index + 1:02}）"
            item["detail"]["title"] = item["source"]["title"]
            self.meta["card_content_selectable"].append({"affair_id": affair, "profile": profile,
                "title": item["source"]["title"]})
        self.oa.persist()
        self.meta["card_content_expiry"] = self.seed_expiry_case()
        self.meta["card_content_discard"] = self.add_pending_case("weekly_report", case_key="content-discard")
        self.save_opinion(self.meta["card_content_discard"], "合成可主动删除的保留文字。")
        self.meta["card_content_discard"]["presentation"] = self.present(self.meta["card_content_discard"])
        self.meta["card_content_queries"] = [self.add_query(), self.add_query()]
        self.meta["card_content_initialized"] = True
        write_metadata(self.meta_path, self.meta)
        write_metadata(self.home / "baseline-ledger.json", self.audit())

    def present(self, case):
        return self.service.present_interaction(user_subject=OWNER, agent_host="openclaw",
            endpoint_key=self.account["endpoint_key"], interaction_id=case["input_interaction"],
            continuous_flow=True)["interaction"]["presentation"]

    def save_opinion(self, case, text):
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(case["input_id"])
        return drafts.save(case["input_id"], csrf_token=opened["csrfToken"], csrf_cookie=opened["csrfToken"],
            expected_revision=opened["state"]["revision"], values={"opinion": text})

    def seed_expiry_case(self):
        old = self.add_pending_case("efficiency_data", case_key="content-expired")
        old["presentation"] = self.present(old)
        saved = self.save_opinion(old, RETAINED_OPINION)
        if not saved.get("contentDraft", {}).get("saved"):
            raise AssertionError("Actual draft save did not retain private content")
        source = self.service.field_submissions.get(old["input_id"])
        original_clock = self.service.field_submissions.clock
        try:
            self.service.field_submissions.clock = lambda: datetime.fromisoformat(source["expires_at"]) + timedelta(seconds=1)
            assert self.service.field_submissions.get(old["input_id"])["state"] == "expired"
        finally:
            self.service.field_submissions.clock = original_clock
        result = self.service.invoke(user_subject=OWNER, capability_name=source["capability_name"],
            arguments=old["arguments"], task_id=old["task_id"], idempotency_key="content-reprepare-" + uuid4().hex)
        if result["status"] != "requires_user_action":
            raise AssertionError(result)
        fresh = {**old, "input_id": result["nextAction"]["inputSubmissionId"],
            "input_interaction": result["interaction"]["interactionId"]}
        fresh["presentation"] = self.present(fresh)
        self.meta["draft_cases"].append(fresh)
        state = BusinessDraftService(self.service).open(fresh["input_id"])["state"]
        if state["values"].get("opinion") or not state.get("contentDraft", {}).get("restore"):
            raise AssertionError("Fresh source must start blank and only offer an explicit content restore")
        return {"old": old, "fresh": fresh, "old_source_expired_via_store_clock": True}

    def add_query(self):
        queued = queue_query(self.service, self.account, {"requestKey": "content-query-" + uuid4().hex})
        result = execute_query(self.service, OWNER, queued["taskId"], queued["commandKey"],
            runtime_context={"endpointId": self.account["endpoint_id"], "hostType": "openclaw",
                "hostInstanceId": "synthetic-content-coordinator"})
        assert result["status"] == "succeeded", result
        return {"task_id": queued["taskId"], "operation_id": result["operationId"]}

    def servers(self):
        cards = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=self.meta["card_port"],
            public_base_url=self.card_url, tls_cert=None, tls_key=None),
            application=TrustedAuthApplication(challenge_store=self.service.challenges, broker=SyntheticBroker(self.service.challenges)),
            field_application=self.flow.applications["input"], action_application=self.flow.applications["authorize"],
            flow_application=self.flow)
        workspace = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1", port=self.meta["workspace_port"],
            public_base_url=self.workspace_url, tls_cert=None, tls_key=None), application=self.app)
        return cards, workspace

    def audit(self):
        with self.service.tasks._connect() as connection:
            count = lambda sql: connection.execute(sql).fetchone()[0]
            return {"decisions": count("SELECT COUNT(*) FROM business_draft_decisions"),
                "authorizations": dict(connection.execute("SELECT state,COUNT(*) FROM write_authorizations GROUP BY state")),
                "syntheticWrites": len(self.oa.writes), "modelDispatches": count("SELECT COUNT(*) FROM agent_host_dispatches"),
                "contentRows": count("SELECT COUNT(*) FROM private_business_draft_contents"),
                "contentActions": [dict(row) for row in connection.execute("SELECT event_type,causation_ref FROM task_events "
                    "WHERE event_type='task.business_content.changed' ORDER BY created_at")],
                "coordinatorErrors": list(self.coordinator_errors)}

    def public_metadata(self):
        return {"fixture": "card-content-real-central-synthetic-oa-v1", "home": str(self.home),
            "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "credentials_file": str(self.credentials_path), "queries": self.meta["card_content_queries"],
            "selectable": self.meta["card_content_selectable"], "expiry": self.meta["card_content_expiry"],
            "discard": self.meta["card_content_discard"], "audit_file": str(self.home / "ledger.json"),
            "evidence_boundary": "Real Central/Auth/Workspace/adapters/decisions; closed synthetic OA; one genuine source expired via its field store clock; no synthetic decisions seeded"}
