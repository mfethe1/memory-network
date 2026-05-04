#!/usr/bin/env node
/**
 * Interactive Sandcastle session for code_index.
 *
 * Usage:
 *   npx tsx .sandcastle/interactive.ts
 *   npx tsx .sandcastle/interactive.ts --agent kimi
 *
 * Launches an interactive agent session inside the sandbox.
 * Useful for exploring the codebase or debugging before an AFK run.
 */

import { interactive, claudeCode, codex } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import { podman } from "@ai-hero/sandcastle/sandboxes/podman";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import type { AnySandboxProvider, AgentProvider } from "@ai-hero/sandcastle";
import { getPlatformConfig, printPlatformBanner } from "./lib/platform.js";
import { kimiCode } from "./lib/kimi-provider.js";

const args = process.argv.slice(2);
const agentFlagIdx = args.indexOf("--agent");
const preferredAgent = agentFlagIdx >= 0 ? args[agentFlagIdx + 1] : undefined;

const preferredProvider = process.env.SANDCASTLE_PROVIDER;
const config = getPlatformConfig(preferredProvider, preferredAgent);
printPlatformBanner(config);

function getProvider(): AnySandboxProvider {
  switch (config.defaultProvider) {
    case "docker":
      return docker(config.providerOptions);
    case "podman":
      return podman(config.providerOptions);
    default:
      return noSandbox();
  }
}

function getAgent(): AgentProvider {
  const model = process.env.MODEL;
  switch (config.defaultAgent) {
    case "claude":
      return claudeCode(model || "claude-sonnet-4-6");
    case "codex":
      return codex(model || "gpt-5.4");
    case "kimi":
      return kimiCode(model || "kimi-k2-0711-preview");
    default:
      throw new Error(`Unknown agent: ${config.defaultAgent}`);
  }
}

async function main() {
  await interactive({
    agent: getAgent(),
    sandbox: getProvider(),
    prompt: "Explore the codebase and understand the architecture.",
  });
}

main().catch((err) => {
  console.error("Interactive session failed:", err);
  process.exit(1);
});
