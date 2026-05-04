import { readFile } from "node:fs/promises";
import process from "node:process";
import type {
  AgentOptions,
  GetRunOptions,
  McpServerConfig,
  Run,
  RunResult,
  SDKAgent,
} from "@cursor/sdk";
import {
  fallbackRunEvents,
  lifecycleFallbackEvent,
  normalizeSdkMessage,
  safeRef,
  sidecarEvent,
  SidecarEmitter,
  type CursorRunRefs,
  type JsonObject,
} from "./events.js";

type CursorSdkModule = typeof import("@cursor/sdk");

export interface SidecarOptions {
  command: string;
  root: string;
  taskJson?: string;
  providerPromptFile?: string;
  mcpConfigFile?: string;
  agentId?: string;
  runId?: string;
  model: string;
  runtime: "local" | "cloud";
  dryRun: "auto" | "dry-run" | "live";
  apiKey?: string;
}

function text(value: unknown): string {
  return String(value ?? "").trim();
}

function parseValueArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      out[key] = next;
      index += 1;
    } else {
      out[key] = true;
    }
  }
  return out;
}

export function parseSidecarOptions(argv: string[]): SidecarOptions {
  const [rawCommand = "run", ...rest] = argv;
  const command = rawCommand === "prompt" ? "run" : rawCommand;
  const values = parseValueArgs(rest);
  const runtime = text(values.runtime || process.env.CODE_INDEX_CURSOR_RUNTIME);
  const dryRun = parseDryRunMode(values["dry-run"], process.env.CODE_INDEX_CURSOR_DRY_RUN);
  return {
    command,
    root: text(values.root) || process.cwd(),
    taskJson: text(values["task-json"]) || undefined,
    providerPromptFile: text(values["provider-prompt-file"]) || undefined,
    mcpConfigFile: text(values["mcp-config-file"]) || undefined,
    agentId: text(values["agent-id"]) || undefined,
    runId: text(values["run-id"]) || undefined,
    model: text(values.model || process.env.CURSOR_MODEL) || "composer-2",
    runtime: runtime === "cloud" ? "cloud" : "local",
    dryRun,
    apiKey: text(values["api-key"] || process.env.CURSOR_API_KEY) || undefined,
  };
}

function parseDryRunMode(
  cliValue: string | boolean | undefined,
  envValue: string | undefined,
): "auto" | "dry-run" | "live" {
  if (cliValue === true) return "dry-run";
  const raw = text(cliValue || envValue).toLowerCase();
  if (!raw || raw === "auto") return "auto";
  if (["1", "true", "yes", "on", "dry-run", "dry_run"].includes(raw)) {
    return "dry-run";
  }
  if (["0", "false", "no", "off", "live"].includes(raw)) {
    return "live";
  }
  return "auto";
}

async function readJson(path?: string): Promise<JsonObject> {
  if (!path) return {};
  const payload = JSON.parse(await readFile(path, "utf8"));
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload as JsonObject)
    : {};
}

async function readPrompt(options: SidecarOptions, task: JsonObject): Promise<string> {
  if (options.providerPromptFile) {
    return readFile(options.providerPromptFile, "utf8");
  }
  return text(task.message);
}

async function readMcpServers(
  path?: string,
): Promise<Record<string, McpServerConfig> | undefined> {
  if (!path) return undefined;
  const payload = await readJson(path);
  const mcpServers = payload.mcpServers;
  return mcpServers && typeof mcpServers === "object" && !Array.isArray(mcpServers)
    ? (mcpServers as Record<string, McpServerConfig>)
    : undefined;
}

function shouldDryRun(options: SidecarOptions, sdk: CursorSdkModule | null): string | undefined {
  if (options.dryRun === "dry-run") return "requested_dry_run";
  if (options.dryRun === "live") return undefined;
  if (!sdk) return "cursor_sdk_unavailable";
  if (!options.apiKey) return "cursor_credentials_unavailable";
  return undefined;
}

async function loadSdk(): Promise<CursorSdkModule | null> {
  try {
    return await import("@cursor/sdk");
  } catch {
    return null;
  }
}

function agentOptions(
  options: SidecarOptions,
  mcpServers?: Record<string, McpServerConfig>,
): AgentOptions {
  const base: AgentOptions = {
    apiKey: options.apiKey,
    model: { id: options.model },
    mcpServers,
  };
  if (options.runtime === "cloud") {
    return { ...base, cloud: {} };
  }
  return { ...base, local: { cwd: options.root } };
}

function runRefs(task: JsonObject, agent: SDKAgent, run?: Run): CursorRunRefs {
  return {
    localRunId: text(task.run_id ?? task.runId),
    cursorAgentId: agent.agentId,
    cursorRunId: run?.id,
  };
}

function existingRunRefs(options: SidecarOptions): CursorRunRefs {
  return {
    cursorAgentId: options.agentId,
    cursorRunId: options.runId,
  };
}

async function emitRunStream(
  run: Run,
  refs: CursorRunRefs,
  emitter: SidecarEmitter,
): Promise<void> {
  if (!run.supports("stream")) {
    emitter.emit(
      sidecarEvent("stream.unsupported", refs, {
        message: run.unsupportedReason("stream") || "Cursor run stream unsupported.",
      }),
    );
    return;
  }
  for await (const message of run.stream()) {
    emitter.emitMany(normalizeSdkMessage(message, refs));
  }
}

function emitRunResult(result: RunResult, refs: CursorRunRefs, emitter: SidecarEmitter): void {
  const statusEvent =
    result.status === "finished"
      ? "run.completed"
      : result.status === "cancelled"
        ? "run.cancelled"
        : "run.failed";
  emitter.emit(
    sidecarEvent(statusEvent, refs, {
      status:
        result.status === "finished"
          ? "completed"
          : result.status === "cancelled"
            ? "cancelled"
            : "failed",
      message: result.result || `Cursor run ${result.status}.`,
      payload: {
        model: result.model,
        duration_ms: result.durationMs,
        git: result.git,
      },
    }),
  );
}

async function runPrompt(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  task: JsonObject,
  emitter: SidecarEmitter,
): Promise<number> {
  const mcpServers = await readMcpServers(options.mcpConfigFile);
  const agent = options.agentId
    ? await sdk.Agent.resume(options.agentId, agentOptions(options, mcpServers))
    : await sdk.Agent.create(agentOptions(options, mcpServers));
  const prompt = await readPrompt(options, task);
  const run = await agent.send(prompt, { mcpServers });
  const refs = runRefs(task, agent, run);
  emitter.emit(
    sidecarEvent("run.started", refs, {
      status: "working",
      message: "Cursor run started.",
    }),
  );
  await emitRunStream(run, refs, emitter);
  emitRunResult(await run.wait(), refs, emitter);
  agent.close();
  emitter.result({ ok: true, cursor_agent_id: agent.agentId, cursor_run_id: run.id });
  return 0;
}

async function createAgent(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  emitter: SidecarEmitter,
): Promise<number> {
  const agent = await sdk.Agent.create(agentOptions(options));
  emitter.emit(
    sidecarEvent("agent.created", { cursorAgentId: agent.agentId }, {
      message: "Cursor agent created.",
    }),
  );
  emitter.result({ ok: true, cursor_agent_id: agent.agentId });
  agent.close();
  return 0;
}

async function getRun(
  sdk: CursorSdkModule,
  options: SidecarOptions,
): Promise<Run> {
  if (!options.runId) {
    throw new Error("--run-id is required");
  }
  const getOptions: GetRunOptions =
    options.runtime === "cloud"
      ? { runtime: "cloud", agentId: options.agentId || "", apiKey: options.apiKey }
      : { runtime: "local", cwd: options.root };
  return sdk.Agent.getRun(options.runId, getOptions);
}

async function streamRun(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  emitter: SidecarEmitter,
): Promise<number> {
  const run = await getRun(sdk, options);
  const refs = {
    cursorAgentId: options.agentId || run.agentId,
    cursorRunId: run.id,
  };
  await emitRunStream(run, refs, emitter);
  emitter.result({ ok: true, cursor_agent_id: refs.cursorAgentId, cursor_run_id: run.id });
  return 0;
}

async function waitRun(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  emitter: SidecarEmitter,
): Promise<number> {
  const run = await getRun(sdk, options);
  const refs = {
    cursorAgentId: options.agentId || run.agentId,
    cursorRunId: run.id,
  };
  emitRunResult(await run.wait(), refs, emitter);
  emitter.result({ ok: true, cursor_agent_id: refs.cursorAgentId, cursor_run_id: run.id });
  return 0;
}

async function cancelRun(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  emitter: SidecarEmitter,
): Promise<number> {
  const run = await getRun(sdk, options);
  const refs = {
    cursorAgentId: options.agentId || run.agentId,
    cursorRunId: run.id,
  };
  await run.cancel();
  emitter.emit(
    sidecarEvent("run.cancelled", refs, {
      status: "cancelled",
      message: "Cursor run cancelled.",
    }),
  );
  emitter.result({ ok: true, cursor_agent_id: refs.cursorAgentId, cursor_run_id: run.id });
  return 0;
}

async function lifecycleAgent(
  sdk: CursorSdkModule,
  options: SidecarOptions,
  emitter: SidecarEmitter,
): Promise<number> {
  if (!options.agentId) {
    throw new Error("--agent-id is required");
  }
  if (options.command === "archive") {
    await sdk.Agent.archive(options.agentId, { cwd: options.root, apiKey: options.apiKey });
  } else if (options.command === "delete") {
    await sdk.Agent.delete(options.agentId, { cwd: options.root, apiKey: options.apiKey });
  } else {
    throw new Error(`unsupported lifecycle command: ${options.command}`);
  }
  emitter.emit(
    sidecarEvent(`agent.${options.command}d`, { cursorAgentId: options.agentId }, {
      message: `Cursor agent ${options.command}d.`,
    }),
  );
  emitter.result({ ok: true, cursor_agent_id: options.agentId });
  return 0;
}

export async function runSidecar(argv: string[] = process.argv.slice(2)): Promise<number> {
  const options = parseSidecarOptions(argv);
  const emitter = new SidecarEmitter();
  const task = await readJson(options.taskJson);
  const sdk = await loadSdk();
  const dryRunReason = shouldDryRun(options, sdk);
  const refs = existingRunRefs(options);

  if (dryRunReason) {
    if (options.command === "run") {
      emitter.emitMany(fallbackRunEvents(task, dryRunReason));
      emitter.result({
        ok: true,
        fallback: "dry-run",
        reason: dryRunReason,
        cursor_agent_id: `cursor-dry-run-${safeRef(task.run_id ?? task.runId)}`,
        cursor_run_id: `cursor-dry-run-${safeRef(task.run_id ?? task.runId)}`,
      });
      return 0;
    }
    emitter.emit(lifecycleFallbackEvent(options.command, refs, dryRunReason));
    emitter.result({
      ok: options.command === "cancel",
      fallback: "unavailable",
      reason: dryRunReason,
      command: options.command,
      cursor_agent_id: options.agentId,
      cursor_run_id: options.runId,
    });
    return 0;
  }

  if (!sdk) {
    throw new Error("Cursor SDK unavailable after dry-run check.");
  }

  if (options.command === "create") return createAgent(sdk, options, emitter);
  if (options.command === "run") return runPrompt(sdk, options, task, emitter);
  if (options.command === "stream") return streamRun(sdk, options, emitter);
  if (options.command === "wait") return waitRun(sdk, options, emitter);
  if (options.command === "cancel") return cancelRun(sdk, options, emitter);
  if (options.command === "archive" || options.command === "delete") {
    return lifecycleAgent(sdk, options, emitter);
  }
  throw new Error(`unknown cursor sidecar command: ${options.command}`);
}
