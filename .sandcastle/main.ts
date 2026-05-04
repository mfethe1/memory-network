#!/usr/bin/env node
/**
 * Cross-platform Sandcastle orchestration for code_index.
 *
 * Usage:
 *   npx tsx .sandcastle/main.ts              # default run
 *   npx tsx .sandcastle/main.ts --plan       # planning mode
 *   npx tsx .sandcastle/main.ts --implement  # implementation mode
 *   npx tsx .sandcastle/main.ts --review     # review mode
 *
 * Supports Windows (WSL2 + Docker Desktop), macOS (Docker Desktop),
 * and Linux (Docker or Podman).
 *
 * Agents: claude (default), codex, kimi - auto-detected from installed CLIs.
 * Override with SANDCASTLE_AGENT=codex or --agent codex.
 */

import { run, claudeCode, codex } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import { podman } from "@ai-hero/sandcastle/sandboxes/podman";
import type { AgentProvider, SandboxProvider } from "@ai-hero/sandcastle";
import { getPlatformConfig, printPlatformBanner } from "./lib/platform.js";
import { kimiCode } from "./lib/kimi-provider.js";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";

// Parse CLI args
const args = process.argv.slice(2);
const mode =
  args.includes("--plan")
    ? "plan"
    : args.includes("--implement")
      ? "implement"
      : args.includes("--review")
        ? "review"
        : "default";

const agentFlagIdx = args.indexOf("--agent");
const preferredAgent = agentFlagIdx >= 0 ? args[agentFlagIdx + 1] : undefined;
const promptFile = argValue("--prompt-file") || process.env.SANDCASTLE_PROMPT_FILE || ".sandcastle/prompt.md";
const taskFile = argValue("--task-file") || process.env.SANDCASTLE_TASK_FILE;
const taskDescriptionOverride =
  argValue("--task") ||
  argValue("--task-description") ||
  process.env.SANDCASTLE_TASK_DESCRIPTION;
const branchOverride = argValue("--branch") || process.env.SANDCASTLE_BRANCH;
const maxIterationsOverride = intArg("--max-iterations", process.env.SANDCASTLE_MAX_ITERATIONS);

const preferredProvider = process.env.SANDCASTLE_PROVIDER;
const config = getPlatformConfig(preferredProvider, preferredAgent);
printPlatformBanner(config);

if (config.defaultProvider === "no-sandbox" && mode !== "default") {
  console.error("No sandbox provider available. Install Docker or Podman first.");
  process.exit(1);
}

// Provider factory
function getProvider(): SandboxProvider {
  switch (config.defaultProvider) {
    case "docker":
      return docker(config.providerOptions);
    case "podman":
      return podman(config.providerOptions);
    case "vercel":
      throw new Error("Vercel provider not yet imported. Add import if needed.");
    default:
      throw new Error(`Unknown provider: ${config.defaultProvider}`);
  }
}

// Agent factory
function getAgent(): AgentProvider {
  const model = process.env.MODEL;
  switch (config.defaultAgent) {
    case "claude":
      return withClaudeSandboxFlags(claudeCode(model || "claude-sonnet-4-6", {
        env: { ANTHROPIC_API_KEY: "" },
        effort: "high",
      }));
    case "codex":
      return codex(model || "gpt-5.4", {
        effort: "high",
      });
    case "kimi":
      return kimiCode(model || "kimi-code/kimi-for-coding", {
        env: providerEnv(["KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL_NAME"]),
        maxSteps: 30,
      });
    default:
      throw new Error(`Unknown agent: ${config.defaultAgent}`);
  }
}

function providerEnv(keys: string[]): Record<string, string> {
  const env: Record<string, string> = {};
  for (const key of keys) {
    const value = process.env[key];
    if (value) env[key] = value;
  }
  return env;
}

function withClaudeSandboxFlags(provider: AgentProvider): AgentProvider {
  return {
    ...provider,
    buildPrintCommand(args) {
      const command = provider.buildPrintCommand(args);
      return {
        ...command,
        command: `${command.command} --setting-sources project,local --strict-mcp-config`,
      };
    },
  };
}

// Mode-specific configuration
interface RunConfig {
  promptFile: string;
  promptArgs: Record<string, string>;
  maxIterations: number;
  branchStrategy: { type: "branch"; branch: string } | { type: "merge-to-head" } | { type: "head" };
  name: string;
}

function argValue(flag: string): string | undefined {
  const index = args.indexOf(flag);
  if (index < 0) return undefined;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) return undefined;
  return value;
}

function intArg(flag: string, fallback?: string): number | undefined {
  const raw = argValue(flag) || fallback;
  if (!raw) return undefined;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

function readTaskDescription(defaultText: string): string {
  if (taskFile) {
    return readFileSync(taskFile, "utf-8").trim();
  }
  return (taskDescriptionOverride || defaultText).trim();
}

function branch(name: string): { type: "branch"; branch: string } {
  return { type: "branch", branch: branchOverride || name };
}

function getRunConfig(): RunConfig {
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");

  switch (mode) {
    case "plan":
      return {
        promptFile,
        promptArgs: {
          TASK_DESCRIPTION:
            readTaskDescription("Analyze the current codebase, identify the most important next feature or fix, and write a detailed implementation plan. Do not write code yet."),
        },
        maxIterations: maxIterationsOverride || 1,
        branchStrategy: branch(`agent/plan-${timestamp}`),
        name: "plan",
      };
    case "implement":
      return {
        promptFile,
        promptArgs: {
          TASK_DESCRIPTION:
            readTaskDescription("Implement the planned feature or fix. Write tests, run them, and commit working code."),
        },
        maxIterations: maxIterationsOverride || 5,
        branchStrategy: branch(`agent/implement-${timestamp}`),
        name: "implement",
      };
    case "review":
      return {
        promptFile,
        promptArgs: {
          TASK_DESCRIPTION:
            readTaskDescription("Review the current branch's changes. Check for bugs, style issues, missing tests, and security concerns. Write a review summary."),
        },
        maxIterations: maxIterationsOverride || 1,
        branchStrategy: branchOverride ? branch(branchOverride) : { type: "merge-to-head" },
        name: "review",
      };
    default:
      return {
        promptFile,
        promptArgs: {
          TASK_DESCRIPTION:
            readTaskDescription("Explore the codebase, understand the architecture, and suggest improvements."),
        },
        maxIterations: maxIterationsOverride || 1,
        branchStrategy: { type: "head" },
        name: "explore",
      };
  }
}

// Main
async function main() {
  const runConfig = getRunConfig();
  const sandbox = getProvider();
  const agent = getAgent();

  console.log(`Mode:       ${mode}`);
  console.log(`Agent:      ${agent.name}`);
  console.log(`Branch:     ${JSON.stringify(runConfig.branchStrategy)}`);
  console.log(`Iterations: ${runConfig.maxIterations}`);
  console.log();

  const result = await run({
    agent,
    sandbox,
    promptFile: runConfig.promptFile,
    promptArgs: runConfig.promptArgs,
    maxIterations: runConfig.maxIterations,
    completionSignal: "<promise>COMPLETE</promise>",
    branchStrategy: runConfig.branchStrategy,
    name: runConfig.name,
    logging: { type: "file", path: `.sandcastle/logs/${runConfig.name}-${Date.now()}.log` },
    hooks: {
      sandbox: {
        onSandboxReady: [
          { command: "if [ -f /home/agent/.kimi-host/kimi.json ]; then cp /home/agent/.kimi-host/kimi.json /home/agent/.kimi/kimi.json; fi" },
          { command: "python3 -m pip install -e . --quiet 2>/dev/null || true" },
        ],
      },
    },
  });

  console.log();
  console.log("=".repeat(60));
  console.log("  Run complete");
  console.log("=".repeat(60));
  console.log(`  Iterations: ${result.iterations.length}`);
  console.log(`  Commits:    ${result.commits.map((c) => c.sha).join(", ") || "none"}`);
  console.log(`  Branch:     ${result.branch}`);
  if (result.logFilePath) {
    console.log(`  Log:        ${resolve(result.logFilePath)}`);
  }
  console.log("=".repeat(60));
}

main().catch((err) => {
  console.error("Sandcastle run failed:", err);
  process.exit(1);
});
