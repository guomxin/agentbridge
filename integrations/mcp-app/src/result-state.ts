type JsonRecord = Record<string, unknown>;
export type Tone = "neutral" | "working" | "success" | "error";

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord : null;
}

/** Interaction completion is not evidence of the business operation's outcome. */
export function operationStatus(result: unknown): string | null {
  const envelope = record(result);
  // Tool transport errors do not establish whether a submitted write took effect.
  if (envelope?.isError === true) return null;
  const structured = record(envelope?.structuredContent);
  if (record(structured?.interaction) || record(record(envelope?._meta)?.["io.agentbridge/interaction"])) return null;
  const status = structured?.status;
  return typeof status === "string" && status.trim() && status !== "completed"
    ? status : null;
}

export function interactionStateLabel(type: string, state: string): string {
  if (state === "completed") {
    return ({credential: "登录已完成", business_input: "信息已提交", execution_authorization: "已授权"})[type]
      || "交互已完成";
  }
  return ({pending: "等待操作", processing: "正在处理", declined: "已取消", expired: "已失效",
    failed: "交互处理失败", superseded: "已被新交互替代"})[state] || state;
}

export function terminalPresentation(result: unknown): {title: string; message: string; label: string; tone: Tone} {
  const status = operationStatus(result);
  if (status === "succeeded") return {
    title: "操作已完成", message: "AgentBridge 已确认业务操作成功。", label: "已完成", tone: "success",
  };
  if (["unknown", "outcome_unknown", "result_unknown"].includes(status || "")) return {
    title: "业务结果待核实", message: "尚未确认业务结果。请先核对原任务，不要重复提交。", label: "结果未知", tone: "error",
  };
  if (status === "failed") return {
    title: "操作未成功", message: "请返回原任务查看失败信息和后续处理方式。", label: "处理失败", tone: "error",
  };
  return {
    title: "操作状态已更新", message: "交互完成不代表业务操作成功，请返回原任务查看核验结果。",
    label: status || "业务结果待确认", tone: "neutral",
  };
}
