// Run by `npm version` (the "version" script): copies the new package.json
// version into src/version.ts and stages it, so the version commit and tag
// include it.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const { version } = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const file = join(root, "src", "version.ts");
const source = readFileSync(file, "utf8");
const updated = source.replace(/export const VERSION = "[^"]*";/, `export const VERSION = "${version}";`);
if (updated === source && !source.includes(`"${version}"`)) {
  console.error(`Could not find the VERSION constant in ${file}.`);
  process.exit(1);
}
writeFileSync(file, updated);
