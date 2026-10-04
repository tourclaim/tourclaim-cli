import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { INTAKE_FIELDS, MEDICAL_FIELDS } from "../src/fields.js";
import { COMMANDS } from "../src/main.js";
import { ATTACHMENT_CONTENT_TYPES, DOC_TYPES, EMAIL_PROVIDERS, INTAKE_STATES, OTHER_INSURANCE, REASON_CATEGORIES } from "../src/types.js";
import { VERSION } from "../src/version.js";
import { ROOT, runCli, tempHome } from "./harness.js";

const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
const openapi = JSON.parse(readFileSync(join(ROOT, "openapi", "connectors-v1.json"), "utf8"));
const schemas = openapi.components.schemas;

describe("package", () => {
  it("has no runtime dependencies and the expected metadata", () => {
    assert.equal(pkg.name, "tourclaim");
    assert.equal(pkg.version, VERSION);
    assert.deepEqual(pkg.bin, { tourclaim: "dist/cli.js" });
    assert.equal(pkg.engines.node, ">=18");
    assert.equal(pkg.license, "MIT");
    assert.equal(pkg.dependencies, undefined);
    assert.deepEqual(Object.keys(pkg.devDependencies).sort(), ["@types/node", "typescript"]);
    assert.ok(pkg.files.includes("dist"));
    assert.ok(!pkg.files.some((f: string) => f.startsWith("src") || f.startsWith("test")));
    assert.match(pkg.repository.url, /github\.com\/tourclaim\/tourclaim-cli/);
  });

  it("starts the built CLI with a node shebang", () => {
    assert.match(readFileSync(join(ROOT, "dist", "cli.js"), "utf8"), /^#!\/usr\/bin\/env node\n/);
  });

  it("uses no em dashes in docs or source", () => {
    const files = ["README.md", "AGENTS.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md"];
    for (const dir of ["src", "src/commands", "test"]) {
      for (const name of readdirSync(join(ROOT, dir))) if (name.endsWith(".ts")) files.push(join(dir, name));
    }
    for (const file of files) {
      assert.ok(!readFileSync(join(ROOT, file), "utf8").includes(String.fromCharCode(0x2014)), `${file} contains an em dash`);
    }
  });
});

describe("types match openapi/connectors-v1.json", () => {
  it("intake fields", () => {
    assert.deepEqual(Object.keys(INTAKE_FIELDS), Object.keys(schemas["IntakeFields-Input"].properties));
    assert.deepEqual(Object.keys(MEDICAL_FIELDS), Object.keys(schemas.MedicalAnswers.properties));
  });

  it("enums", () => {
    assert.deepEqual([...REASON_CATEGORIES], schemas.ReasonCategory.enum);
    const otherInsurance = schemas["IntakeFields-Input"].properties.other_insurance.anyOf.find((s: { enum?: string[] }) => s.enum);
    assert.deepEqual([...OTHER_INSURANCE], otherInsurance.enum);
    assert.deepEqual([...INTAKE_STATES], schemas.IntakeResponse.properties.state.enum);
    assert.deepEqual([...DOC_TYPES], schemas.AttachmentEvidence.properties.doc_type.enum);
    assert.deepEqual([...ATTACHMENT_CONTENT_TYPES], schemas.AttachmentEvidence.properties.content_type.enum);
    assert.deepEqual([...EMAIL_PROVIDERS].sort(), [...schemas.EmailEvidence.properties.provider.enum].sort());
  });

  it("paths the CLI calls exist", () => {
    const paths = Object.keys(openapi.paths);
    for (const p of [
      "/api/connectors/v1/cards",
      "/api/connectors/v1/intakes",
      "/api/connectors/v1/intakes/{intake_id}",
      "/api/connectors/v1/intakes/{intake_id}/email-evidence",
      "/api/connectors/v1/intakes/{intake_id}/attachments",
      "/api/connectors/v1/intakes/{intake_id}/submit",
      "/api/connectors/v1/claims",
      "/api/connectors/v1/claims/{claim_id}",
    ]) {
      assert.ok(paths.includes(p), p);
    }
  });
});

describe("help", () => {
  it("every command has --help, exiting 0 without network", async () => {
    const { home, cleanup } = await tempHome();
    try {
      for (const cmd of COMMANDS) {
        const r = await runCli([...cmd.path, "--help"], { home, apiUrl: "http://127.0.0.1:9" });
        assert.equal(r.code, 0, cmd.path.join(" "));
        assert.match(r.stdout, new RegExp(`^Usage: tourclaim ${cmd.path.join(" ")}`));
      }
      for (const argv of [[], ["--help"], ["help"], ["help", "intake", "attach"], ["intake", "--help"], ["cards"], ["help", "whoami"]]) {
        const r = await runCli(argv, { home });
        assert.equal(r.code, 0, argv.join(" "));
        assert.match(r.stdout, /Usage: tourclaim/);
      }
      const v = await runCli(["--version"], { home });
      assert.equal(v.stdout, `${VERSION}\n`);
      const vj = await runCli(["--version", "--json"], { home });
      assert.deepEqual(vj.json(), { name: "tourclaim", version: VERSION });
    } finally {
      await cleanup();
    }
  });

  it("unknown commands exit 2, as JSON in --json mode", async () => {
    const { home, cleanup } = await tempHome();
    try {
      const r = await runCli(["frobnicate", "--json"], { home });
      assert.equal(r.code, 2);
      assert.equal(r.jsonError().code, "usage_error");
      assert.equal(r.stdout, "");
    } finally {
      await cleanup();
    }
  });
});
