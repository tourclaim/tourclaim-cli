// Compiles src/ to dist/ and marks the CLI entry executable.
import { spawnSync } from "node:child_process";
import { chmodSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tsc = createRequire(import.meta.url).resolve("typescript/bin/tsc");

rmSync(join(root, "dist"), { recursive: true, force: true });
const result = spawnSync(process.execPath, [tsc, "-p", join(root, "tsconfig.json")], { stdio: "inherit" });
if (result.status !== 0) process.exit(result.status ?? 1);
chmodSync(join(root, "dist", "cli.js"), 0o755);
