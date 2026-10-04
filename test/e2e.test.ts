// Drives the real dist/cli.js in child processes against the mock API, the
// way a person or an agent would: sign in, fill a draft, add evidence, have
// the traveler sign in "their browser", submit and list claims.
import assert from "node:assert/strict";
import { readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { spawnCli, tempHome, type CliResult } from "./harness.js";
import { MockServer } from "./mock-server.js";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1EAAAAASUVORK5CYII=", "base64");

describe("end to end (dist/cli.js)", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;
  const outputs: CliResult[] = [];

  before(async () => {
    mock = new MockServer({ deviceInterval: 1, enforceDeviceInterval: true });
    await mock.start();
    ({ home, cleanup } = await tempHome());
  });
  after(async () => {
    await mock.stop();
    await cleanup();
  });

  const env = () => ({ TOURCLAIM_API_URL: mock.url });
  async function run(argv: string[], stdin?: string): Promise<CliResult> {
    const r = await spawnCli(argv, { home, env: env(), ...(stdin !== undefined ? { stdin } : {}) }).done;
    outputs.push(r);
    return r;
  }

  it("login, start, set, attach, add-email, sign --wait, submit, claims list, logout", { timeout: 60_000 }, async () => {
    // Sign in. The first JSON line is what an agent relays to its human.
    const login = spawnCli(["login", "--json", "--no-browser"], { home, env: env() });
    const first = JSON.parse(await login.firstLine);
    assert.equal(first.event, "device_code");
    // The traveler opens the link in their browser and approves.
    assert.equal((await fetch(first.verification_uri_complete)).status, 200);
    const signedIn = await login.done;
    outputs.push(signedIn);
    assert.equal(signedIn.code, 0, signedIn.stderr);
    assert.equal(signedIn.json().event, "signed_in");
    assert.equal(signedIn.json().account_email, "pat@example.com");

    const credentials = join(home, ".config", "tourclaim", "credentials.json");
    if (process.platform !== "win32") assert.equal((await stat(credentials)).mode & 0o777, 0o600);
    const key: string = JSON.parse(await readFile(credentials, "utf8"))[mock.url].api_key;
    assert.match(key, /^tc_muse_/);

    const status = await run(["status"]);
    assert.equal(status.code, 0, status.stderr);
    assert.match(status.stdout, /^REVIEW MODE/);

    const cards = await run(["cards", "search", "synthetic", "--json"]);
    assert.equal(cards.json()[0].id, 900);

    const started = await run(["intake", "start", "--json", "--set", "merchant_name=Synthetic Harbor Tours", "--set", "reason_category=weather"]);
    assert.equal(started.code, 0, started.stderr);
    const id: string = started.json().id;
    assert.equal(started.json().state, "collecting");
    const drafts = await run(["intake", "list", "--json"]);
    assert.deepEqual(drafts.json().map((d: { id: string }) => d.id), [id]);

    const set = await run([
      "intake",
      "set",
      id,
      "booking_ref=REVIEW-0001",
      "trip_date=2026-10-01",
      "booking_amount=620.00",
      "refunded_amount=0.00",
      "currency=USD",
      "card_product_id=900",
      "narrative=Fictional tour cancelled by a storm warning. No real travel data.",
      "other_insurance=no",
    ]);
    assert.equal(set.code, 0, set.stderr);
    assert.match(set.stdout, /State:\s+needs_approval/);

    const receipt = join(home, "receipt.png");
    await writeFile(receipt, PNG);
    const refused = await run(["intake", "attach", id, receipt, "--type", "receipt"]);
    assert.equal(refused.code, 2, "attach without --yes and without a terminal must refuse");
    assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).length, 0);
    const attached = await run(["intake", "attach", id, receipt, "--type", "receipt", "--yes", "--json"]);
    assert.equal(attached.code, 0, attached.stderr);
    assert.equal(attached.json().evidence.length, 1);

    const eml = join(home, "cancel.eml");
    await writeFile(eml, "From: ops@harbor.test\nSubject: Tour cancelled\nDate: Wed, 30 Sep 2026 08:00:00 +0000\n\nYour tour on 1 October is cancelled due to the storm warning.\n");
    const emailed = await run(["intake", "add-email", id, "--eml", eml, "--yes"]);
    assert.equal(emailed.code, 0, emailed.stderr);
    assert.match(emailed.stdout, /2 evidence items/);

    // Wait for the signature; the traveler signs in their browser meanwhile.
    const sign = spawnCli(["intake", "sign", id, "--wait", "--json", "--no-browser"], { home, env: env() });
    const waiting = JSON.parse(await sign.firstLine);
    assert.equal(waiting.event, "waiting_for_signature");
    assert.equal((await fetch(waiting.review_url)).status, 200);
    const signed = await sign.done;
    outputs.push(signed);
    assert.equal(signed.code, 0, signed.stderr);
    assert.equal(signed.json().state, "ready_to_submit");

    const submitted = await run(["intake", "submit", id, "--json"]);
    assert.equal(submitted.code, 0, submitted.stderr);
    const claim = submitted.json();
    assert.equal(claim.status, "INTAKE_RECEIVED");
    assert.equal(claim.mode, "review");

    const list = await run(["claims", "list", "--json"]);
    assert.deepEqual(list.json().map((c: { id: string }) => c.id), [claim.id]);

    const logout = await run(["logout", "--json"]);
    assert.equal(logout.code, 0, logout.stderr);
    assert.equal(logout.json().revoked, true);
    const after = await run(["status", "--json"]);
    assert.equal(after.code, 3);

    for (const r of outputs) {
      assert.ok(!r.stdout.includes(key) && !r.stderr.includes(key), "the key must never be printed");
    }
  });

  it("reads a key piped to login --with-token", async () => {
    const other = await tempHome();
    try {
      const key = mock.issueKey({ traveler: "sam@example.com" });
      const r = await spawnCli(["login", "--with-token"], { home: other.home, env: env(), stdin: `${key}\n` }).done;
      assert.equal(r.code, 0, r.stderr);
      assert.match(r.stdout, /Signed in as sam@example\.com/);
      assert.ok(!r.stdout.includes(key) && !r.stderr.includes(key));
    } finally {
      await other.cleanup();
    }
  });

  it("prints help and the version from the binary", async () => {
    const help = await run(["--help"]);
    assert.equal(help.code, 0);
    assert.match(help.stdout, /Usage: tourclaim <command>/);
    const version = await run(["--version"]);
    assert.match(version.stdout, /^\d+\.\d+\.\d+\n$/);
    assert.equal(version.stderr, "", "no warnings on stderr");
  });
});
