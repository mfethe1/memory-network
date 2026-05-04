/**
 * Custom Sandcastle agent provider for Kimi Code CLI.
 *
 * Kimi is not shipped with @ai-hero/sandcastle, so we implement the
 * AgentProvider contract manually.
 */

import type { AgentProvider, PrintCommand } from "@ai-hero/sandcastle";

type ParsedStreamEvent = ReturnType<AgentProvider["parseStreamLine"]>[number];

const shellEscape = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'";

const TOOL_NAME_MAP: Record<string, string> = {
  Shell: "Bash",
  ReadFile: "ReadFile",
  WriteFile: "WriteFile",
  EditFile: "EditFile",
  Grep: "Grep",
  Glob: "Glob",
  FetchURL: "FetchURL",
  SearchWeb: "SearchWeb",
};

function parseKimiStreamLine(line: string): ParsedStreamEvent[] {
  if (!line.startsWith("{")) return [];
  try {
    const obj = JSON.parse(line);

    // Assistant turn
    if (obj.role === "assistant" && Array.isArray(obj.content)) {
      const events: ParsedStreamEvent[] = [];
      const texts: string[] = [];

      for (const block of obj.content) {
        if (block.type === "text" && typeof block.text === "string") {
          texts.push(block.text);
        }
      }

      if (texts.length > 0) {
        events.push({ type: "text", text: texts.join("") });
      }

      // Tool calls
      if (Array.isArray(obj.tool_calls)) {
        for (const tc of obj.tool_calls) {
          const rawName = tc.function?.name;
          const rawArgs = tc.function?.arguments;
          if (typeof rawName !== "string" || typeof rawArgs !== "string") continue;

          const mappedName = TOOL_NAME_MAP[rawName] ?? rawName;
          // For Shell/Bash, try to extract the command from JSON args
          let displayArgs = rawArgs;
          if (mappedName === "Bash") {
            try {
              const parsed = JSON.parse(rawArgs);
              if (typeof parsed.command === "string") displayArgs = parsed.command;
            } catch {
              // keep rawArgs
            }
          }
          events.push({ type: "tool_call", name: mappedName, args: displayArgs });
        }
      }

      return events;
    }

    // Tool result turn - we don't need to emit events for these,
    // but we can use them to know the agent is still working
    if (obj.role === "tool" && Array.isArray(obj.content)) {
      return [];
    }
  } catch {
    // Not valid JSON
  }
  return [];
}

export interface KimiCodeOptions {
  /** Environment variables injected by this agent provider. */
  readonly env?: Record<string, string>;
  /** Maximum steps per turn. */
  readonly maxSteps?: number;
}

export const kimiCode = (model: string, options?: KimiCodeOptions): AgentProvider => ({
  name: "kimi-code",
  env: options?.env ?? {},
  captureSessions: false,

  buildPrintCommand({ prompt, dangerouslySkipPermissions }): PrintCommand {
    const yolo = dangerouslySkipPermissions ? " --yolo" : "";
    const modelFlag = ` -m ${shellEscape(model)}`;
    const maxStepsFlag = options?.maxSteps ? ` --max-steps-per-turn ${options.maxSteps}` : "";
    return {
      command: `kimi --print${yolo} --output-format stream-json${modelFlag}${maxStepsFlag} -p ${shellEscape(prompt)}`,
    };
  },

  buildInteractiveArgs({ prompt, dangerouslySkipPermissions }): string[] {
    const args = ["kimi"];
    if (dangerouslySkipPermissions) args.push("--yolo");
    args.push("--model", model);
    if (prompt) args.push("-p", prompt);
    return args;
  },

  parseStreamLine(line: string): ParsedStreamEvent[] {
    return parseKimiStreamLine(line);
  },
});
