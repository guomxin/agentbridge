"""Published private skills and real terminal tasks for Workspace feedback QA.

Only the method sample and task output are synthetic. Publication, immutable
bindings, task transitions, ownership and feedback use the production stores.
No gateway send, model call or downstream business capability is executed.
"""
from uuid import uuid4

from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER
from tests.support.interaction_acceptance import write_metadata
from tests.test_workspace import PASSWORD


PROPOSAL = {
    "name": "合成结果核对助手", "description": "从输入中区分已完成、未完成和无法确认的事项。",
    "selection": {"use_when": "核对输入任务的结果", "not_for": "提交任何真实业务表单", "output": "明确的结果与证据边界"},
    "instructions": "仅依据输入文字。无法确认的结果保持未知。用户反馈不是业务成功证据。输出完成、未完成、待核对三类事实。",
    "profiles": {"use": {}},
}


class TaskFeedbackAcceptanceFixture(BusinessViewAcceptanceFixture):
    def __init__(self, home, *, seed=True):
        super().__init__(home, seed=False)
        self.meta.setdefault("feedback_cases", [])
        self.meta.setdefault("feedback_publications", [])
        if seed and not self.meta["feedback_cases"]:
            self.add_feedback_cases()
        write_metadata(self.meta_path, self.meta)

    def _publish(self, draft, key):
        authoring = self.service.skill_authoring
        sample = authoring.test_start(OWNER, draft_id=draft["draft_id"], expected_revision=draft["revision"],
            profile="use", prompt="合成样例：任务A已完成，任务B的办理结果尚无法确认。", request_key=f"feedback-sample:{key}")
        authoring.test_result(OWNER, test_id=sample["test_id"], output="合成输出：任务A已完成；任务B待核对，不能据此重复提交。")
        request = authoring.submit(OWNER, draft_id=draft["draft_id"], expected_revision=draft["revision"],
            request_key=f"feedback-submit:{key}", reason="隔离 Workspace 反馈验收，个人私有助手", audience=[OWNER])
        return authoring.decide(actor={"role": "admin", "username": "synthetic-acceptance-reviewer"},
            request_id=request["request_id"], decision="approve", reason="仅发布隔离夹具的合成方法",
            reviewed_tests=True, manual_quality_reason="合成测试替身已核对样例文本；不表示独立模型评测或真实业务完成。")

    def add_feedback_cases(self, case_key=None):
        key = case_key or uuid4().hex[:8]
        authoring = self.service.skill_authoring
        draft = authoring.save(OWNER, proposal={**PROPOSAL, "name": f"合成结果核对助手 · {key}"},
                               request_key=f"feedback-create:{key}")
        original = self._publish(draft, f"{key}:v1")
        cases = []
        for state in ("succeeded", "failed", "outcome_unknown", "active", "unbound"):
            title = {"succeeded": "已完成", "failed": "未完成", "outcome_unknown": "结果待核对", "active": "进行中", "unbound": "无助手绑定"}[state]
            task_id = self.service.ensure_host_task(user_subject=OWNER, token_id="fixture-token", agent_host="openclaw",
                host_task_key=f"feedback-acceptance|{key}|{state}", endpoint_key=self.account["endpoint_key"], client_type="web",
                external_subject=self.account["account_id"], conversation_ref=self.account["openclaw_session_key"],
                title=f"助手反馈验收 · {title} · {key}")["task"]["taskId"]
            binding = None
            if state != "unbound":
                binding = self.service.skills.bind(OWNER, draft["skill_id"], "use", expected_version=original["published_version"])
                self.service.skills.attach(OWNER, task_id, binding)
            self.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=self.account["endpoint_id"],
                message_key=f"feedback:{key}:{state}", role="assistant",
                text=f"合成纯文本任务：{title}。业务助手版本 1.0.0；反馈仅代表使用感受，不证明任何业务写入成功。", task_id=task_id)
            if state in {"succeeded", "unbound"}:
                self.service.finish_host_task(user_subject=OWNER, task_id=task_id, outcome="succeeded", reason="synthetic_text_task_completed")
            elif state == "failed":
                self.service.finish_host_task(user_subject=OWNER, task_id=task_id, outcome="failed", error_code="SYNTHETIC_NOT_COMPLETED",
                                              message="合成任务未完成，没有下游业务提交。")
            elif state == "outcome_unknown":
                self.service.tasks.mark_task_outcome_unknown(task_id=task_id, user_subject=OWNER,
                    error_code="SYNTHETIC_OUTCOME_NOT_CONFIRMED")
            case = {"key": key, "state": state, "task_id": task_id, "binding_id": binding,
                    "skill_id": draft["skill_id"], "version": original["published_version"], "profile": "use"}
            cases.append(case)
        updated = authoring.save(OWNER, draft_id=draft["draft_id"], expected_revision=draft["revision"],
            proposal={**PROPOSAL, "name": f"合成结果核对助手 · {key}", "instructions": PROPOSAL["instructions"] + " 新版增加证据来源说明。"},
            request_key=f"feedback-update:{key}")
        latest = self._publish(updated, f"{key}:v2")
        self.meta["feedback_cases"].extend(cases)
        self.meta["feedback_publications"].append({"key": key, "draft_id": draft["draft_id"], "skill_id": draft["skill_id"],
            "task_version": original["published_version"], "current_version": latest["published_version"]})
        write_metadata(self.meta_path, self.meta)
        return cases

    def revoke_current_skill(self):
        skill_id = self.meta["feedback_publications"][-1]["skill_id"]
        current = self.service.skills.config(f"user:{OWNER}")
        values = {key: value for key, value in current["value"].items() if key != skill_id}
        return self.service.skills.save(f"user:{OWNER}", values, expected_revision=current["revision"],
            actor="synthetic-acceptance-reviewer", reason="验证历史反馈不会恢复已撤销助手执行权限")

    def add_pending_business_cases(self):
        """Append independent input tasks; existing effects and receipts stay put."""
        cases = self.add_view_cases()
        cases.append({**self.add_case(), "profile": "work_log"})
        self.meta.setdefault("feedback_business_cases", []).extend(cases)
        write_metadata(self.meta_path, self.meta)
        return cases

    def public_metadata(self):
        return {"fixture": "actual-task-feedback-synthetic-method-v1", "workspace_url": self.workspace_url,
            "card_origin": self.card_url, "username": "cardreview", "other_username": "cardother", "synthetic_password": PASSWORD,
            "publication": self.meta["feedback_publications"][-1], "cases": self.meta["feedback_cases"][-5:],
            "business_cases": [{**case, "flow_url": f"{self.card_url}/flow/input/{case['input_id']}"}
                               for case in self.meta.get("feedback_business_cases", [])[-4:]],
            "oa_write_count": len(self.oa.writes), "taihua_write_count": self.transport.write_calls,
            "evidence_boundary": "Real private publication/binding/task/feedback; synthetic text output, no model or downstream write"}
