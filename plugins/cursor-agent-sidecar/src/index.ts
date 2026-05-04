#!/usr/bin/env node

import process from "node:process";
import { runSidecar } from "./run.js";

runSidecar()
  .then((exitCode) => {
    process.exitCode = exitCode;
  })
  .catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`cursor-agent-sidecar: ${message}\n`);
    process.stdout.write(
      `${JSON.stringify({
        provider: "cursor",
        event: "run.failed",
        status: "failed",
        message,
        payload: { error: message },
      })}\n`,
    );
    process.exitCode = 1;
  });
