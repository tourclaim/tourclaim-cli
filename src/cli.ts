#!/usr/bin/env node
// Entry point. Kept free of static imports so the version check below runs
// before any module that needs newer Node features is loaded.

const [major = 0, minor = 0] = process.versions.node.split(".").map(Number);
if (major < 18 || (major === 18 && minor < 3) || typeof fetch !== "function") {
  process.stderr.write(`tourclaim needs Node.js 18.3 or newer (this is ${process.version}).\n`);
  process.exit(1);
}

// Node 18 and 20 print an ExperimentalWarning the first time fetch is used.
// Drop that one warning and pass every other warning to Node's own handlers.
const warningListeners = process.listeners("warning");
process.removeAllListeners("warning");
process.on("warning", (warning) => {
  if (warning.name === "ExperimentalWarning" && /fetch/i.test(warning.message)) return;
  for (const listener of warningListeners) listener.call(process, warning);
});

// `tourclaim schema | head` closes stdout early; that is not an error.
process.stdout.on("error", (error: NodeJS.ErrnoException) => {
  if (error.code === "EPIPE") process.exit(0);
  throw error;
});

const { run } = await import("./main.js");
const { realDeps } = await import("./deps.js");
process.exitCode = await run(process.argv.slice(2), realDeps());

export {};
