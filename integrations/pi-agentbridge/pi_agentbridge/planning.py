"""A single central planning-control receipt belongs to this SDK run and endpoint."""
import json
import time

from agentbridge.core.planning_policy import planning_capability_for_tool
from nanobot_agentbridge.plan_submission import PlanSubmission
from nanobot_agentbridge.task_context import TaskContext

PLAN = "agentbridge_task_plan_prepare"
CATALOG = "agentbridge_task_plan_catalog"
NEXT = "oa_workflow_history_next"


class ReportTask(TaskContext):
    def __init__(self, original, plan_id):
        request = {**original.request, "runId":original.request["runId"] + ":plan-report"}
        super().__init__(original.session, request, original.host_meta, host_type="pi", verify_telegram=original.verify_telegram)
        self.plan_id = plan_id

    def ensure_arguments(self):
        return {**super().ensure_arguments(), "tool_name":NEXT, "planning_report_plan_id":self.plan_id}


class PlanningRepair:
    def __init__(self, central):
        self.central = central
        self.source = None
        self.trigger = None
        self.expires = 0
        self.catalog_read = False
        self.used = False
        self.cursor = None
        self.query_id = None
        self.read_tools = set()
        self.context = None
        self.report_task = None
        self.public = None
        self.plan_state = None
        self.report_complete = False
        self.report_expected = False

    def capture(self, payload, original, tool):
        if not isinstance(payload, dict) or payload.get("status") != "planning_control" or (
                payload.get("error") or {}).get("code") != "PLAN_REQUIRED":
            return None
        if not original or payload.get("taskId") != original or self.source:
            raise PermissionError("Central planning control did not match this original task")
        self.source, self.trigger, self.expires = original, tool, time.monotonic() + 300
        return "PLAN_REQUIRED。原任务要求持久计划。先读取本人计划目录，再按原请求提交一次计划。请勿重复调用原工具或扩大办理范围。"

    def visible(self, names):
        if time.monotonic() >= self.expires:
            return []
        permitted = {NEXT} if self.cursor else set() if self.used else {CATALOG, PLAN}
        return sorted(permitted.intersection(names))

    async def preflight(self, name):
        context = self.central.task_context
        if not context.meta or (name != PLAN and not planning_capability_for_tool(name)):
            return None
        source = context.meta["io.agentbridge/task"]["taskId"]
        args = {**context.ensure_arguments(), "task_scope":"host_run", "tool_name":name,
                "planning_task_key":context.request["runId"]}
        found = await self.central.session.call_tool("agentbridge_host_task_ensure", args, meta=self.central.meta)
        data = found.structuredContent or {}
        if found.isError:
            raise PermissionError("Planning preflight unavailable")
        signal = self.capture(data, source, name)
        if not signal and ((data.get("task") or {}).get("taskId") != source
                           or (data.get("endpoint") or {}).get("endpointId") != context.request["endpoint"]):
            raise PermissionError("Planning preflight returned a different task")
        return signal

    async def handle(self, name, arguments):
        central = self.central
        if name not in self.visible(central.definitions):
            return "原任务已要求持久计划。此调用不属于原计划的续办范围。"
        if name == CATALOG:
            # The central catalog applies current subject grants and fixed schema revisions.
            result = await central.session.call_tool(name, arguments, meta=central.meta)
            data = result.structuredContent or {}
            if result.isError or not data.get("catalogRevision") or data.get("status") == "rejected":
                return "本人的计划目录未获核验。"
            self.catalog_read = True
            return json.dumps(data, ensure_ascii=False)
        if name == NEXT:
            if arguments != {"cursor": self.cursor}:
                return "只能使用原计划签发的当前报告游标。"
            self.cursor = None
            result = await central.session.call_tool(name, arguments, meta=await self.report_task.get())
            data = result.structuredContent or {}
            page = data.get("result") or {}
            if result.isError or data.get("status") != "succeeded" or page.get("query_id") != self.query_id:
                return "原计划报告页未获核验，请查看原任务。"
            self.cursor = page.get("next_cursor") if page.get("has_more") is True else None
            self.report_complete = self.cursor is None and page.get("has_more") is False
            return json.dumps(page, ensure_ascii=False)
        if not self.catalog_read:
            return "请先核对本人计划目录中的能力和转换。"
        self.used = True
        original = central.task_context
        if central.skill and (central.skill.selection or central.skill.pending):
            return "当前助手绑定原任务，本轮未获独立计划的助手授权。"
        context = TaskContext(central.session, original.request, central.meta, host_type="pi",
                              verify_telegram=original.verify_telegram, repair_source=self.source)
        self.context = context
        plan = central.plan
        # A read's control receipt cannot add a write sink. A blocked prepare may
        # use only that same reviewed sink, which still goes through trusted cards.
        prepares = {self.trigger:central.governed[self.trigger]} if self.trigger in central.governed else {}
        sources = self.read_tools | {self.trigger}
        if sources.intersection({"oa_workflow_done_list", "oa_workflow_sent_list"}):
            sources.add("oa_workflow_history_query")
        sub = PlanSubmission(plan.request, context, plan.definition, plan.scope, plan.allowed_model_tools.intersection(sources), prepares,
                             host_type="pi", govern=plan.govern, observe=self.observe)
        central.task_context = context
        try:
            answer = await sub.execute(**arguments)
        finally:
            if not central.governed_result:
                central.task_context = original
        return self.public or answer

    def observe(self, data):
        plan = data["plan"]
        self.plan_state = plan.get("state")
        if plan.get("terminalReason") == "PLAN_SOURCE_INCOMPLETE":
            self.cursor = None
            self.public = "PLAN_SOURCE_INCOMPLETE。来源不完整，原计划已停止，未提交业务写入。"
            return
        action = data.get("nextAction") or {}
        projection = plan.get("resultProjection") or {}
        self.report_expected = projection.get("kind") == "oa_history_report"
        report = projection.get("result") or {}
        if (plan.get("state") == "succeeded" and projection.get("kind") == "oa_history_report"
                and report.get("read_only") is True and isinstance(report.get("query_id"), str)
                and action.get("readOnly") is True and action.get("tool") == NEXT):
            cursor = (action.get("arguments") or {}).get("cursor")
            if isinstance(cursor, str) and cursor and cursor in (report.get("replay_cursor"), report.get("next_cursor")):
                self.query_id, self.cursor = report["query_id"], cursor
                self.report_task = ReportTask(self.context, plan["planId"])
                self.public = json.dumps({"计划状态":plan["state"], "nextAction":{"tool":NEXT,"arguments":{"cursor":cursor}}},
                                         ensure_ascii=False)

    def final_answer(self, model_text):
        if self.plan_state != "succeeded":
            return self.public or "中央已要求持久计划，本轮尚未核验到原计划完成。"
        if self.report_expected and not self.report_complete:
            return "原计划已完成，报告页尚未全部取回。"
        return model_text
