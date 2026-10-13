import { App } from "@modelcontextprotocol/ext-apps";
import { interactionStateLabel, operationStatus, terminalPresentation } from "./result-state";
import "./styles.css";

type JsonRecord = Record<string, unknown>;

type Interaction = {
  schemaVersion: string;
  interactionId: string;
  type: "credential" | "business_input" | "execution_authorization";
  state: string;
  title: string;
  message: string;
  display?: JsonRecord;
  presentation: {
    url: string;
  };
  poll?: {
    recommendedIntervalSeconds?: number;
  };
  resume?: {
    ready?: boolean;
    completed?: boolean;
  };
};

const PRIVATE_INTERACTION_META_KEY = "io.agentbridge/interaction";
const TERMINAL_STATES = new Set([
  "declined",
  "expired",
  "failed",
  "superseded",
]);
const ACTION_LABELS: Record<Interaction["type"], string> = {
  credential: "安全登录",
  business_input: "填写信息",
  execution_authorization: "确认执行",
};

const titleElement = requiredElement<HTMLHeadingElement>("title");
const messageElement = requiredElement<HTMLParagraphElement>("message");
const factsElement = requiredElement<HTMLDListElement>("facts");
const statusElement = requiredElement<HTMLParagraphElement>("status");
const actionButton = requiredElement<HTMLButtonElement>("primary-action");

const app = new App({ name: "AgentBridge Trusted Interaction", version: "0.1.0" });
let currentInteraction: Interaction | null = null;
let pollTimer: number | undefined;
let requestInFlight = false;
let reportedInteractionId: string | null = null;

app.ontoolresult = (result) => {
  void handleToolResult(result);
};

actionButton.addEventListener("click", () => {
  void openTrustedSurface();
});

await app.connect();

async function handleToolResult(result: unknown): Promise<void> {
  const interaction = extractInteraction(result);
  if (interaction) {
    currentInteraction = interaction;
    renderInteraction(interaction);

    if (interaction.resume?.ready) {
      await resumeInteraction(interaction);
      return;
    }
    if (isTerminal(interaction)) {
      stopPolling();
      await reportCompletion(interaction, result);
      return;
    }
    if (interaction.state === "processing") {
      schedulePoll(interaction);
    }
    return;
  }

  renderTerminalResult(result);
}

async function openTrustedSurface(): Promise<void> {
  const interaction = currentInteraction;
  if (!interaction || requestInFlight) return;

  requestInFlight = true;
  actionButton.disabled = true;
  setStatus("正在请求宿主打开安全页面", "working");
  try {
    const opened = await app.openLink({ url: interaction.presentation.url });
    if (opened.isError) {
      setStatus("宿主未允许打开安全页面", "error");
      actionButton.disabled = false;
      return;
    }
    setStatus("安全页面已打开，等待操作完成", "working");
    schedulePoll(interaction, 250);
  } catch {
    setStatus("无法打开安全页面", "error");
    actionButton.disabled = false;
  } finally {
    requestInFlight = false;
  }
}

async function pollInteraction(interaction: Interaction): Promise<void> {
  if (requestInFlight || currentInteraction?.interactionId !== interaction.interactionId) {
    return;
  }
  requestInFlight = true;
  let result: unknown;
  try {
    result = await app.callServerTool({
      name: "agentbridge_interaction_get",
      arguments: { interaction_id: interaction.interactionId },
    });
  } catch {
    setStatus("状态检查暂时失败，正在重试", "error");
    schedulePoll(interaction);
    return;
  } finally {
    requestInFlight = false;
  }
  await handleToolResult(result);
}

async function resumeInteraction(interaction: Interaction): Promise<void> {
  if (requestInFlight) {
    schedulePoll(interaction, 400);
    return;
  }
  requestInFlight = true;
  stopPolling();
  setStatus("信息已接收，正在继续原操作", "working");
  try {
    const result = await app.callServerTool({
      name: "agentbridge_interaction_resume",
      arguments: {
        interaction_id: interaction.interactionId,
        idempotency_key: `mcp-app:${interaction.interactionId}`,
      },
    });
    const nextInteraction = extractInteraction(result);
    if (nextInteraction) {
      currentInteraction = nextInteraction;
      renderInteraction(nextInteraction);
      if (nextInteraction.resume?.ready) {
        requestInFlight = false;
        await resumeInteraction(nextInteraction);
      } else if (isTerminal(nextInteraction)) {
        await reportCompletion(nextInteraction, result);
      }
      return;
    }
    renderTerminalResult(result);
    await reportCompletion(interaction, result);
  } catch {
    setStatus("续跑失败，可稍后重试", "error");
    actionButton.hidden = true;
  } finally {
    requestInFlight = false;
  }
}

function renderInteraction(interaction: Interaction): void {
  titleElement.textContent = interaction.title || "AgentBridge 可信交互";
  messageElement.textContent =
    interaction.message || "请在 AgentBridge 安全页面完成本次操作。";
  renderFacts(interaction);
  const stateLabel = interactionStateLabel(interaction.type, interaction.state);
  const tone = TERMINAL_STATES.has(interaction.state)
    ? interaction.state === "declined" ? "neutral" : "error"
    : interaction.resume?.ready
      ? "working"
      : "neutral";
  setStatus(stateLabel, tone);

  const canOpen = ["pending", "processing"].includes(interaction.state);
  actionButton.hidden = !canOpen;
  actionButton.disabled = interaction.state === "processing";
  actionButton.textContent = ACTION_LABELS[interaction.type];

  if (interaction.state === "processing") {
    schedulePoll(interaction);
  }
}

function renderFacts(interaction: Interaction): void {
  const display = asRecord(interaction.display);
  const rows: Array<[string, string]> = [];
  if (typeof display?.systemName === "string") {
    rows.push(["系统", display.systemName]);
  }
  if (typeof display?.fieldCount === "number") {
    rows.push(["字段", String(display.fieldCount)]);
  }
  rows.push(["交互编号", shortId(interaction.interactionId)]);

  factsElement.replaceChildren();
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    factsElement.append(term, detail);
  }
  factsElement.hidden = false;
}

function renderTerminalResult(result: unknown): void {
  const presentation = terminalPresentation(result);
  titleElement.textContent = presentation.title;
  messageElement.textContent = presentation.message;
  factsElement.hidden = true;
  actionButton.hidden = true;
  setStatus(presentation.label, presentation.tone);
  stopPolling();
}

async function reportCompletion(interaction: Interaction, result: unknown): Promise<void> {
  if (reportedInteractionId === interaction.interactionId) return;
  reportedInteractionId = interaction.interactionId;

  const status = operationStatus(result);
  const context = {
    interactionId: interaction.interactionId,
    interactionType: interaction.type,
    interactionState: interaction.state,
    operationStatus: status,
  };
  try {
    await app.updateModelContext({
      content: [
        {
          type: "text",
          text: `AgentBridge interaction state: ${interaction.state}. Business operation status: ${status ?? "not confirmed"}. Observe the original task; interaction completion is not business success. Do not replay a submitted operation.`,
        },
      ],
      structuredContent: { agentbridgeInteraction: context },
    });
    if (app.getHostCapabilities()?.message) {
      await app.sendMessage({
        role: "user",
        content: [
          {
            type: "text",
            text: "AgentBridge 可信交互状态已更新，请查看原任务的核验结果；交互完成不代表业务成功，不要重复提交。",
          },
        ],
      });
    }
  } catch {
    setStatus(interactionStateLabel(interaction.type, interaction.state), "neutral");
  }
}

function schedulePoll(interaction: Interaction, delay?: number): void {
  stopPolling();
  const seconds = interaction.poll?.recommendedIntervalSeconds;
  const interval = Number.isFinite(seconds) ? Math.max(1, Number(seconds)) * 1000 : 2000;
  pollTimer = window.setTimeout(() => {
    void pollInteraction(interaction);
  }, delay ?? interval);
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearTimeout(pollTimer);
    pollTimer = undefined;
  }
}

function extractInteraction(result: unknown): Interaction | null {
  const record = asRecord(result);
  const meta = asRecord(record?._meta);
  const privateValue = meta?.[PRIVATE_INTERACTION_META_KEY];
  if (isInteraction(privateValue)) return privateValue;

  const structured = asRecord(record?.structuredContent);
  const publicValue = structured?.interaction;
  return isInteraction(publicValue) ? publicValue : null;
}

function isInteraction(value: unknown): value is Interaction {
  const record = asRecord(value);
  const presentation = asRecord(record?.presentation);
  return (
    record?.schemaVersion === "agentbridge.interaction.v1" &&
    typeof record.interactionId === "string" &&
    typeof record.type === "string" &&
    typeof record.state === "string" &&
    typeof record.title === "string" &&
    typeof record.message === "string" &&
    typeof presentation?.url === "string" &&
    presentation.url.startsWith("https://")
  );
}

function isTerminal(interaction: Interaction): boolean {
  return Boolean(interaction.resume?.completed) || TERMINAL_STATES.has(interaction.state);
}

function setStatus(text: string, tone: "neutral" | "working" | "success" | "error"): void {
  statusElement.textContent = text;
  statusElement.dataset.tone = tone;
}

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value;
}

function requiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing element: ${id}`);
  return element as T;
}
