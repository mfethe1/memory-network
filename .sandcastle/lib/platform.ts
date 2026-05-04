/**
 * Cross-platform detection for Sandcastle orchestration.
 * Supports Windows (WSL2/Docker Desktop), macOS (Docker Desktop), Linux (Docker/Podman).
 */

import { execSync } from "node:child_process";
import { platform, homedir } from "node:os";
import { resolve } from "node:path";
import { existsSync } from "node:fs";

export type Platform = "win32" | "darwin" | "linux";
export type SandboxProviderName = "docker" | "podman" | "vercel" | "no-sandbox";
export type AgentName = "claude" | "codex" | "kimi";

export interface PlatformConfig {
  platform: Platform;
  isWsl: boolean;
  isDockerDesktop: boolean;
  defaultProvider: SandboxProviderName;
  defaultAgent: AgentName;
  providerOptions: Record<string, unknown>;
  cwd: string;
  warnings: string[];
}

function mountIfExists(
  hostPath: string,
  sandboxPath: string,
  readonly = false
): { hostPath: string; sandboxPath: string; readonly?: boolean } | null {
  return existsSync(hostPath) ? { hostPath, sandboxPath, readonly } : null;
}

function detectWsl(): boolean {
  if (platform() !== "win32" && platform() !== "linux") return false;
  try {
    const release = execSync("cat /proc/sys/kernel/osrelease", {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
    });
    return release.toLowerCase().includes("microsoft") || release.toLowerCase().includes("wsl");
  } catch {
    return false;
  }
}

function detectDockerDesktop(): boolean {
  try {
    const info = execSync("docker info --format '{{.Name}}'", {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 5000,
    });
    return info.toLowerCase().includes("desktop");
  } catch {
    return false;
  }
}

function detectProvider(preferred?: string): SandboxProviderName {
  if (preferred) return preferred as SandboxProviderName;

  // Check for explicit override
  const envProvider = process.env.SANDCASTLE_PROVIDER;
  if (envProvider) return envProvider as SandboxProviderName;

  // Detect available providers
  const providers: SandboxProviderName[] = ["docker", "podman"];
  for (const p of providers) {
    try {
      execSync(`${p} --version`, { stdio: "ignore", timeout: 5000 });
      return p;
    } catch {
      // not available
    }
  }

  // Fallback for interactive only
  return "no-sandbox";
}

function detectAgent(preferred?: string): AgentName {
  if (preferred) return preferred as AgentName;

  const envAgent = process.env.SANDCASTLE_AGENT;
  if (envAgent) return envAgent as AgentName;

  // Detect available agent CLIs
  const agents: [AgentName, string][] = [
    ["claude", "claude"],
    ["kimi", "kimi"],
    ["codex", "codex"],
  ];
  for (const [name, cmd] of agents) {
    try {
      execSync(`${cmd} --version`, { stdio: "ignore", timeout: 5000 });
      return name;
    } catch {
      // not available
    }
  }

  return "claude"; // fallback
}

export function getPlatformConfig(preferredProvider?: string, preferredAgent?: string): PlatformConfig {
  const plat = platform() as Platform;
  const isWsl = detectWsl();
  const isDockerDesktop = detectDockerDesktop();
  const provider = detectProvider(preferredProvider);
  const agent = detectAgent(preferredAgent);
  const warnings: string[] = [];

  // WSL-specific guidance
  if (plat === "win32" && !isWsl) {
    warnings.push(
      "Running on Windows without WSL. Docker Desktop with WSL2 backend is strongly recommended for Sandcastle."
    );
  }

  if (plat === "win32" && isWsl) {
    warnings.push(
      "WSL detected. For best performance, keep your repo inside the WSL filesystem (e.g., ~/projects/) rather than /mnt/"
    );
  }

  if (provider === "no-sandbox") {
    warnings.push(
      "No container runtime detected. Install Docker Desktop or Podman, or use 'vercel' for cloud sandboxing."
    );
  }

  const config: PlatformConfig = {
    platform: plat,
    isWsl,
    isDockerDesktop,
    defaultProvider: provider,
    defaultAgent: agent,
    providerOptions: {
      imageName: "sandcastle:code-index",
      // Mount package manager caches and auth directories for faster installs
      // and subscription-based CLI access (Claude, Kimi, Codex)
      mounts: [
        mountIfExists(resolve(homedir(), ".npm"), "/home/agent/.npm", true),
        mountIfExists(resolve(homedir(), ".cache/pip"), "/home/agent/.cache/pip", true),
        // Claude Code auth (subscription login)
        mountIfExists(resolve(homedir(), ".claude"), "/home/agent/.claude", false),
        mountIfExists(resolve(homedir(), ".claude.json"), "/home/agent/.claude.json", false),
        // Kimi auth (subscription login). Mount auth/config files, but leave logs
        // container-local to avoid Windows bind-mount rename failures during log rotation.
        mountIfExists(resolve(homedir(), ".kimi/credentials"), "/home/agent/.kimi/credentials", false),
        mountIfExists(resolve(homedir(), ".kimi/config.toml"), "/home/agent/.kimi/config.toml", false),
        mountIfExists(resolve(homedir(), ".kimi/device_id"), "/home/agent/.kimi/device_id", false),
        mountIfExists(resolve(homedir(), ".kimi/kimi.json"), "/home/agent/.kimi-host/kimi.json", true),
        // Codex auth. Keep sessions/logs container-local so the CLI can write
        // Linux-owned state even when host auth files come from Windows.
        mountIfExists(resolve(homedir(), ".codex/auth.json"), "/home/agent/.codex/auth.json", false),
        mountIfExists(resolve(homedir(), ".codex/config.toml"), "/home/agent/.codex/config.toml", false),
        // OpenAI config (for Codex API key fallback)
        mountIfExists(resolve(homedir(), ".config/openai"), "/home/agent/.config/openai", false),
      ].filter(Boolean) as Array<{ hostPath: string; sandboxPath: string; readonly?: boolean }>,
    },
    cwd: process.cwd(),
    warnings,
  };

  return config;
}

export function printPlatformBanner(config: PlatformConfig): void {
  console.log("=".repeat(60));
  console.log("  Sandcastle Cross-Platform Orchestration");
  console.log("=".repeat(60));
  console.log(`  OS:           ${config.platform}${config.isWsl ? " (WSL)" : ""}`);
  console.log(`  Docker:       ${config.isDockerDesktop ? "Docker Desktop" : "Engine"}`);
  console.log(`  Provider:     ${config.defaultProvider}`);
  console.log(`  Agent:        ${config.defaultAgent}`);
  console.log(`  Working dir:  ${config.cwd}`);
  if (config.warnings.length > 0) {
    console.log("  Warnings:");
    for (const w of config.warnings) {
      console.log(`    WARNING: ${w}`);
    }
  }
  console.log("=".repeat(60));
  console.log();
}
