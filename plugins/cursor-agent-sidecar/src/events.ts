import type { SDKMessage } from "@cursor/sdk";

export type JsonObject = Record<string, unknown>;

export interface CursorRunRefs {
  localRunId?: string;
  cursorAgentId?: string;
  cursorRunId?: string;
}

export interface CursorSidecarEvent {
  provider: "cursor";
  event: string;
  local_run_id?: string;
  cursor_agent_id?: string;
  cursor_run_id?: string;
  message?: string;
  status?: string;
  role?: string;
  tool_name?: string;
  arguments?: JsonObject;
  output?: unknown;
  payload?: JsonObject;
}

export function safeRef(value: unknown, fallback = "task"): string {
  const text = String(value ?? "").trim() || fallback;
  return text.replace(/[^A-Za-z0-9_-]/g, "_");
}

export function eventRefs(refs: CursorRunRefs): Pick<
  CursorSidecarEvent,
  "local_run_id" | "cursor_agent_id" | "cursor_run_id"
> {
  return {
    ...(refs.localRunId ? { local_run_id: refs.localRunId } : {}),
    ...(refs.cursorAgentId ? { cursor_agent_id: refs.cursorAgentId } : {}),
    ...(refs.cursorRunId ? { cursor_run_id: refs.cursorRunId } : {}),
  };
}

export function sidecarEvent(
  event: string,
  refs: CursorRunRefs,
  values: Omit<CursorSidecarEvent, "provider" | "event"> = {},
): CursorSidecarEvent {
  return {
    provider: "cursor",
    event,
    ...eventRefs(refs),
    ...values,
  };
}

export function fallbackRunRefs(task: JsonObject): CursorRunRefs {
  const localRunId = String(task.run_id ?? task.runId ?? "").trim();
  const cursorRef = `cursor-dry-run-${safeRef(localRunId)}`;
  return {
    localRunId,
    cursorAgentId: cursorRef,
    cursorRunId: cursorRef,
  };
}

export function fallbackRunEvents(
  task: JsonObject,
  reason: string,
): CursorSidecarEvent[] {
  const refs = fallbackRunRefs(task);
  const selectedPaths = Array.isArray(task.selected_paths)
    ? task.selected_paths.map((value) => String(value ?? "").trim()).filter(Boolean)
    : [];
  const payload = { fallback: "dry-run", reason };
  return [
    sidecarEvent("run.started", refs, {
      status: "working",
      message: "Cursor sidecar dry-run started.",
      payload,
    }),
    ...selectedPaths.map((filePath) =>
      sidecarEvent("tool.call", refs, {
        tool_name: "Read",
        arguments: { file_path: filePath },
        message: `Cursor sidecar dry-run inspected ${filePath}.`,
        payload,
      }),
    ),
    sidecarEvent("assistant.message", refs, {
      role: "assistant",
      message: "Cursor sidecar dry-run completed without contacting Cursor.",
      payload,
    }),
    sidecarEvent("run.completed", refs, {
      status: "completed",
      message: "Cursor sidecar dry-run completed task.",
      payload,
    }),
  ];
}

export function lifecycleFallbackEvent(
  command: string,
  refs: CursorRunRefs,
  reason: string,
): CursorSidecarEvent {
  if (command === "cancel") {
    return sidecarEvent("run.cancelled", refs, {
      status: "cancelled",
      message: "Cursor sidecar recorded cancellation without contacting Cursor.",
      payload: { fallback: "dry-run", reason },
    });
  }
  return sidecarEvent("lifecycle.unavailable", refs, {
    status: "unavailable",
    message: `Cursor sidecar ${command} unavailable: ${reason}.`,
    payload: { fallback: "unavailable", command, reason },
  });
}

export function terminalStatus(event: CursorSidecarEvent): string | undefined {
  const status = String(event.status ?? "").toLowerCase();
  if (["completed", "failed", "cancelled", "canceled"].includes(status)) {
    return status;
  }
  if (event.event === "run.completed") return "completed";
  if (event.event === "run.failed") return "failed";
  if (event.event === "run.cancelled" || event.event === "run.canceled") {
    return "cancelled";
  }
  return undefined;
}

export class SidecarEmitter {
  private terminalEmitted = false;

  emit(event: CursorSidecarEvent): boolean {
    const terminal = terminalStatus(event);
    if (terminal) {
      if (this.terminalEmitted) return false;
      this.terminalEmitted = true;
    }
    process.stdout.write(`${JSON.stringify(event)}\n`);
    return true;
  }

  emitMany(events: CursorSidecarEvent[]): void {
    for (const event of events) {
      this.emit(event);
    }
  }

  result(payload: JsonObject): void {
    process.stdout.write(
      `${JSON.stringify({ provider: "cursor", event: "command.result", ...payload })}\n`,
    );
  }
}

function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function text(value: unknown): string {
  return String(value ?? "").trim();
}

function sdkRefs(message: JsonObject, refs: CursorRunRefs): CursorRunRefs {
  return {
    localRunId: refs.localRunId,
    cursorAgentId: text(message.agent_id ?? message.agentId) || refs.cursorAgentId,
    cursorRunId: text(message.run_id ?? message.runId) || refs.cursorRunId,
  };
}

function statusEventName(status: string): string {
  switch (status.toUpperCase()) {
    case "FINISHED":
      return "run.completed";
    case "ERROR":
    case "EXPIRED":
      return "run.failed";
    case "CANCELLED":
      return "run.cancelled";
    case "CREATING":
    case "RUNNING":
    default:
      return "run.started";
  }
}

function localStatus(status: string): string {
  switch (status.toUpperCase()) {
    case "FINISHED":
      return "completed";
    case "ERROR":
    case "EXPIRED":
      return "failed";
    case "CANCELLED":
      return "cancelled";
    case "CREATING":
    case "RUNNING":
    default:
      return "working";
  }
}

export function normalizeSdkMessage(
  message: SDKMessage | JsonObject,
  refs: CursorRunRefs,
): CursorSidecarEvent[] {
  const payload = asObject(message);
  const type = text(payload.type);
  const nextRefs = sdkRefs(payload, refs);

  if (type === "status") {
    const status = text(payload.status);
    return [
      sidecarEvent(statusEventName(status), nextRefs, {
        status: localStatus(status),
        message: text(payload.message) || `Cursor status: ${status || "unknown"}.`,
      }),
    ];
  }

  if (type === "assistant") {
    const sdkMessage = asObject(payload.message);
    const content = Array.isArray(sdkMessage.content) ? sdkMessage.content : [];
    const events: CursorSidecarEvent[] = [];
    for (const block of content) {
      const item = asObject(block);
      if (item.type === "text") {
        const value = text(item.text);
        if (value) {
          events.push(
            sidecarEvent("assistant.message", nextRefs, {
              role: "assistant",
              message: value,
            }),
          );
        }
      }
      if (item.type === "tool_use") {
        events.push(
          sidecarEvent("tool.call", nextRefs, {
            tool_name: text(item.name) || "tool",
            arguments: asObject(item.input),
            message: `Cursor requested tool call: ${text(item.name) || "tool"}`,
          }),
        );
      }
    }
    return events;
  }

  if (type === "tool_call") {
    return [
      sidecarEvent("tool.call", nextRefs, {
        tool_name: text(payload.name) || "tool",
        arguments: asObject(payload.args),
        output: payload.result,
        payload: { tool_status: text(payload.status) },
        message: `Cursor tool ${text(payload.name) || "tool"} ${text(payload.status) || "updated"}.`,
      }),
    ];
  }

  if (type === "thinking") {
    return [
      sidecarEvent("thinking.message", nextRefs, {
        message: text(payload.text) || "Cursor is thinking.",
      }),
    ];
  }

  if (type === "system") {
    return [
      sidecarEvent("run.started", nextRefs, {
        status: "working",
        message: "Cursor SDK stream initialized.",
        payload: {
          model: payload.model,
          tools: Array.isArray(payload.tools) ? payload.tools : [],
        },
      }),
    ];
  }

  return [];
}
