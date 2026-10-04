// Compiles src/ and test/ to .build/ and runs every test file with node:test.
// Expects dist/ to be built already (npm test does that first): the
// end-to-end tests run the real dist/cli.js.
import { spawnSync } from "node:child_process";
import { readdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tsc = createRequire(import.meta.url).resolve("typescript/bin/tsc");
const out = join(root, ".build");

rmSync(out, { recursive: true, force: true });
const build = spawnSync(process.execPath, [tsc, "-p", join(root, "tsconfig.test.json")], { stdio: "inherit" });
if (build.status !== 0) process.exit(build.status ?? 1);
if (process.argv.includes("--build-only")) process.exit(0);

const testDir = join(out, "test");
const files = readdirSync(testDir)
  .filter((name) => name.endsWith(".test.js"))
  .sort()
  .map((name) => join(testDir, name));
const result = spawnSync(process.execPath, ["--test", ...files], { stdio: "inherit", cwd: root });
process.exit(result.status ?? 1);
