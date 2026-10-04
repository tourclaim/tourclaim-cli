import assert from "node:assert/strict";
import { readFile, stat, writeFile, mkdir, chmod } from "node:fs/promises";
import { dirname, join } from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { CredentialStore } from "../src/credentials.js";
import { credPath, runCli, tempHome } from "./harness.js";
import { MockServer } from "./mock-server.js";

const isWindows = process.platform === "win32";
const TOKEN = "/api/connectors/device/token";

describe("login (device flow)", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;

  before(async () => {
    mock = new MockServer({ deviceInterval: 5 });
    await mock.start();
  });
  after(() => mock.stop());
  beforeEach(async () => {
    ({ home, cleanup } = await tempHome());
    mock.deviceScript = [];
    mock.inject = [];
    mock.rateLimit.remaining = 0;
  });
  afterEach(() => cleanup());

  it("polls until approved, honoring interval and slow_down, and saves a 0600 credential", async () => {
    mock.deviceScript = ["pending", "slow_down", "pending", "approve"];
    const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    // 5 s, 5 s, then slow_down adds 5 s for every later poll.
    assert.deepEqual(r.sleeps, [5000, 5000, 10000, 10000]);
    assert.match(r.stdout, /open this page:\n\n  http:\/\/127\.0\.0\.1:\d+\/connect\/cli\n/);
    assert.match(r.stdout, /^Enter this code on that page: [A-Z]{4}-[A-Z]{4}$/m);
    assert.doesNotMatch(r.stdout, /\?code=/, "the code is never put in a link");
    assert.match(r.stdout, /Signed in as pat@example\.com \(key expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC, in (29|30) days\)/);
    assert.match(r.stdout, /Review mode: claims are synthetic and nothing is filed/);
    assert.deepEqual(r.opened, [], "--no-browser must not open anything");

    const path = credPath(home);
    const saved = JSON.parse(await readFile(path, "utf8"));
    const entry = saved[mock.url];
    assert.match(entry.api_key, /^tc_muse_[A-Za-z0-9_-]{48}$/);
    assert.equal(typeof entry.expires_at, "string");
    assert.equal(typeof entry.grant_id, "string");
    assert.deepEqual(entry.scopes, ["intakes:write", "evidence:write", "claims:submit", "claims:read"]);
    if (!isWindows) {
      assert.equal((await stat(path)).mode & 0o777, 0o600);
      assert.equal((await stat(dirname(credPath(home)))).mode & 0o777, 0o700);
    }
    assert.ok(!r.stdout.includes(entry.api_key) && !r.stderr.includes(entry.api_key), "the key must never be printed");
  });

  it("sends the client string and requested scopes, and the User-Agent", async () => {
    mock.deviceScript = ["approve"];
    const before = mock.requests.length;
    const r = await runCli(["login", "--no-browser", "--scope", "intakes:write,claims:read"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const codeReq = mock.requestsTo("POST", "/api/connectors/device/code").at(-1);
    assert.deepEqual(codeReq?.body, { client: `tourclaim-cli/0.1.0 (${process.platform} x64; node 20.0.0)`, scopes: ["intakes:write", "claims:read"] });
    for (const req of mock.requests.slice(before)) {
      assert.match(String(req.headers["user-agent"]), /^tourclaim-cli\/0\.1\.0 \(\w+ x64; node 20\.0\.0\)$/);
    }
  });

  it("prints the code as a JSON line first in --json mode, then the result, and never the device_code", async () => {
    mock.deviceScript = ["pending", "approve"];
    const r = await runCli(["login", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const [first, last, ...rest] = r.jsonLines();
    assert.equal(rest.length, 0);
    assert.equal(first.event, "device_code");
    assert.match(first.user_code, /^[A-Z]{4}-[A-Z]{4}$/);
    assert.equal(first.verification_uri, `${mock.url}/connect/cli`);
    assert.deepEqual(Object.keys(first).sort(), ["event", "expires_in", "interval", "user_code", "verification_uri"]);
    assert.equal(first.expires_in, 600);
    assert.ok(!("device_code" in first));
    const device = mock.device(first.user_code);
    assert.ok(device && !r.stdout.includes(device.deviceCode));
    assert.equal(last.event, "signed_in");
    assert.equal(last.account_email, "pat@example.com");
    assert.equal(last.mode, "review");
    assert.equal(last.already_signed_in, false);
    assert.ok(last.key.id && last.key.expires_at && Array.isArray(last.key.scopes));
    assert.ok(!JSON.stringify(last).includes("tc_muse_"));
  });

  it("opens the browser only when stdout is a terminal", async () => {
    mock.deviceScript = ["approve"];
    let r = await runCli(["login"], { home, apiUrl: mock.url, stdoutIsTTY: true });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.opened.length, 1);
    assert.equal(r.opened[0], `${mock.url}/connect/cli`);

    const other = await tempHome();
    mock.deviceScript = ["approve"];
    r = await runCli(["login"], { home: other.home, apiUrl: mock.url, stdoutIsTTY: false });
    assert.equal(r.opened.length, 0);
    await other.cleanup();
  });

  it("exits 3 when the traveler declines", async () => {
    mock.deviceScript = ["pending", "denied"];
    const r = await runCli(["login", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 3);
    assert.equal(r.jsonError().code, "access_denied");
    assert.match(r.jsonError().message, /declined/);
    assert.equal(await new CredentialStore(credPath(home), process.platform, () => {}).get(mock.url), null);
  });

  it("exits 3 when the code expired or was already used", async () => {
    mock.deviceScript = ["expired"];
    const r = await runCli(["login"], { home, apiUrl: mock.url });
    assert.equal(r.code, 3);
    assert.match(r.stderr, /expired or was already used/);
  });

  it("stops at expires_in after one final poll at the deadline", async () => {
    const short = new MockServer({ deviceInterval: 5, deviceExpiresIn: 12 });
    await short.start();
    try {
      const r = await runCli(["login"], { home, apiUrl: short.url });
      assert.equal(r.code, 3);
      // Never sleeps past the deadline; the poll at the deadline is the last one.
      assert.deepEqual(r.sleeps, [5000, 5000, 2000]);
      assert.equal(short.requestsTo("POST", "/api/connectors/device/token").length, 3);
      assert.match(r.stderr, /expired before it was approved/);
    } finally {
      await short.stop();
    }
  });

  it("collects an approval made in the last seconds", async () => {
    const short = new MockServer({ deviceInterval: 5, deviceExpiresIn: 12 });
    await short.start();
    try {
      short.deviceScript = ["pending", "pending", "approve"];
      const r = await runCli(["login", "--no-browser"], { home, apiUrl: short.url });
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.sleeps, [5000, 5000, 2000]);
    } finally {
      await short.stop();
    }
  });

  it("a 503 from the token poll is temporary: it waits Retry-After, then the key signs in", async () => {
    mock.inject.push({ status: 503, body: { detail: "Service busy" }, headers: { "Retry-After": "1" }, path: TOKEN });
    mock.deviceScript = ["approve"];
    const r = await runCli(["login", "--no-browser", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.deepEqual(r.sleeps, [5000, 1000]);
    assert.equal(r.json().event, "signed_in");
  });

  it("a 502 or 504 without Retry-After waits the interval, and Retry-After never stretches it", async () => {
    mock.inject.push({ status: 502, body: "<html>Bad Gateway</html>", path: TOKEN });
    mock.inject.push({ status: 503, body: { detail: "Service busy" }, headers: { "Retry-After": "30" }, path: TOKEN });
    mock.deviceScript = ["approve"];
    const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.deepEqual(r.sleeps, [5000, 5000, 5000]);
  });

  it("temporary failures past the limit still end the sign-in", async () => {
    for (let i = 0; i < 3; i++) mock.inject.push({ status: 503, body: { detail: "Service busy" }, headers: { "Retry-After": "1" }, path: TOKEN });
    const r = await runCli(["login", "--no-browser", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 7);
    assert.equal(r.jsonError().code, "unavailable");
    assert.equal(r.jsonError().retry_after, 1);
    assert.deepEqual(r.sleeps, [5000, 1000, 1000]);
  });

  it("a temporary failure is retried and a later success counts the failures from zero", async () => {
    mock.inject.push({ status: 503, body: { detail: "Service busy" }, headers: { "Retry-After": "1" }, path: TOKEN });
    mock.inject.push({ status: 504, body: "", path: TOKEN });
    mock.deviceScript = ["pending", "approve"];
    let polls = 0;
    mock.onRequest = (req) => {
      if (req.path === "/api/connectors/device/token" && ++polls === 4) {
        mock.inject.push({ status: 502, body: "", path: TOKEN }, { status: 503, body: { detail: "Service busy" }, path: TOKEN });
      }
    };
    try {
      const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
      assert.equal(r.code, 0, r.stderr);
      // 503 (1 s), 504 (interval), pending resets the count, 502 and 503 (interval each), then the key.
      assert.deepEqual(r.sleeps, [5000, 1000, 5000, 5000, 5000, 5000]);
    } finally {
      mock.onRequest = null;
    }
  });

  it("the poll at the deadline is the last one even when it meets a 503", async () => {
    const short = new MockServer({ deviceInterval: 5, deviceExpiresIn: 12 });
    await short.start();
    let polls = 0;
    short.onRequest = (req) => {
      if (req.path === "/api/connectors/device/token" && ++polls === 3) short.inject.push({ status: 503, body: { detail: "Service busy" }, path: TOKEN });
    };
    try {
      const r = await runCli(["login", "--no-browser"], { home, apiUrl: short.url });
      assert.equal(r.code, 7);
      assert.deepEqual(r.sleeps, [5000, 5000, 2000]);
      assert.equal(short.requestsTo("POST", "/api/connectors/device/token").length, 3);
    } finally {
      await short.stop();
    }
  });

  it("sends no key with the polls when none is stored", async () => {
    mock.deviceScript = ["pending", "approve"];
    const before = mock.requests.length;
    const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const polls = mock.requests.slice(before).filter((q) => q.path === "/api/connectors/device/token");
    assert.equal(polls.length, 2);
    for (const q of polls) assert.equal(q.headers.authorization, undefined);
  });

  it("treats a 429 while polling like slow_down", async () => {
    mock.deviceScript = [];
    let polls = 0;
    mock.onRequest = (req) => {
      if (req.path === "/api/connectors/device/token") {
        polls++;
        if (polls === 1) mock.rateLimit = { remaining: 1, retryAfter: "1" };
        if (polls === 3) mock.deviceScript = ["approve"];
      }
    };
    try {
      const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
      assert.equal(r.code, 0, r.stderr);
      // The 429 adds 5 s, like slow_down.
      assert.deepEqual(r.sleeps, [5000, 10000, 10000]);
    } finally {
      mock.onRequest = null;
    }
  });

  it("ignores verification_uri_complete from older servers", async () => {
    const old = new MockServer({ deviceInterval: 5, sendCompleteUri: true });
    await old.start();
    try {
      old.deviceScript = ["approve"];
      const r = await runCli(["login", "--json"], { home, apiUrl: old.url, stdoutIsTTY: true });
      assert.equal(r.code, 0, r.stderr);
      assert.ok(!("verification_uri_complete" in r.jsonLines()[0]));
      assert.deepEqual(r.opened, [`${old.url}/connect/cli`]);
      assert.doesNotMatch(r.stdout, /\?code=/);
    } finally {
      await old.stop();
    }
  });

  it("at the 5-key limit the server retires the oldest login key; --force does not warn about it", async () => {
    const traveler = "busy@example.com";
    const oldest = mock.issueKey({ traveler, channel: "cli" });
    for (let i = 0; i < 3; i++) mock.issueKey({ traveler, channel: "cli" });
    const muse = mock.issueKey({ traveler, channel: "key" });
    await new CredentialStore(credPath(home), process.platform, () => {}).set(mock.url, { api_key: oldest, expires_at: null, grant_id: null, scopes: [] });
    mock.deviceScript = ["approve"];
    mock.deviceTraveler = traveler;
    try {
      const r = await runCli(["login", "--force", "--no-browser"], { home, apiUrl: mock.url });
      assert.equal(r.code, 0, r.stderr);
      assert.doesNotMatch(r.stderr, /warning/);
      assert.match(r.stdout, /The server retired the previous key [0-9a-f-]{36}\./);
      assert.equal(mock.keyRecord(oldest)?.revoked, true);
      assert.equal(mock.keyRecord(muse)?.revoked, false, "keys made for other apps are never retired");
    } finally {
      mock.deviceTraveler = null;
    }
  });

  it("shows the server's reason when every connection belongs to another app", async () => {
    const traveler = "full@example.com";
    for (let i = 0; i < 5; i++) mock.issueKey({ traveler, channel: "key" });
    mock.deviceScript = ["approve"];
    mock.deviceTraveler = traveler;
    try {
      const r = await runCli(["login", "--json", "--no-browser"], { home, apiUrl: mock.url });
      assert.equal(r.code, 3);
      assert.equal(r.jsonError().code, "access_denied");
      assert.equal(
        r.jsonError().message,
        "This account already has five connections. Disconnect one at /connect/muse, then run `tourclaim login` again. Nothing was saved.",
      );
    } finally {
      mock.deviceTraveler = null;
    }
  });

  it("reports an existing valid key instead of starting a new sign-in", async () => {
    const key = mock.issueKey({ channel: "cli" });
    await new CredentialStore(credPath(home), process.platform, () => {}).set(mock.url, {
      api_key: key,
      expires_at: null,
      grant_id: null,
      scopes: [],
    });
    const before = mock.requestsTo("POST", "/api/connectors/device/code").length;
    const r = await runCli(["login", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.json().already_signed_in, true);
    assert.equal(r.json().account_email, "pat@example.com");
    assert.equal(mock.requestsTo("POST", "/api/connectors/device/code").length, before);
  });

  it("--force sends the stored key with each poll; the server retires it and the tool does not DELETE it", async () => {
    const oldKey = mock.issueKey({ channel: "cli" });
    const store = new CredentialStore(credPath(home), process.platform, () => {});
    await store.set(mock.url, { api_key: oldKey, expires_at: null, grant_id: null, scopes: [] });
    mock.deviceScript = ["pending", "approve"];
    const before = mock.requests.length;
    const r = await runCli(["login", "--force", "--no-browser", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const after = mock.requests.slice(before);
    const polls = after.filter((q) => q.path === "/api/connectors/device/token");
    assert.equal(polls.length, 2);
    for (const q of polls) assert.equal(q.headers.authorization, `Bearer ${oldKey}`);
    assert.equal(after.filter((q) => q.method === "DELETE").length, 0);
    assert.equal(mock.keyRecord(oldKey)?.revoked, true);
    assert.notEqual((await store.get(mock.url))?.api_key, oldKey);
    assert.equal(r.json().replaced_key_id, mock.keyRecord(oldKey)?.id);
    assert.equal(r.json().previous_key_active, false);
    assert.ok(!r.stdout.includes(oldKey) && !r.stderr.includes(oldKey));
    mock.deviceScript = ["approve"];
    const human = await runCli(["login", "--force", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(human.code, 0, human.stderr);
    assert.match(human.stdout, /The server retired the previous key [0-9a-f-]{36}\./);
  });

  it("--force over a key the server does not retire says it still works", async () => {
    const pasted = mock.issueKey({ channel: "key" });
    await new CredentialStore(credPath(home), process.platform, () => {}).set(mock.url, { api_key: pasted, expires_at: null, grant_id: null, scopes: [] });
    mock.deviceScript = ["approve"];
    const r = await runCli(["login", "--force", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(mock.keyRecord(pasted)?.revoked, false);
    assert.match(r.stderr, /warning: The previous key [0-9a-f-]{36} still works/);
  });

  it("a stored key that no longer works is still sent, and a 401 for it falls back to no key", async () => {
    const strict = new MockServer({ deviceInterval: 5, rejectBadBearer: true });
    await strict.start();
    try {
      const dead = strict.issueKey({ channel: "cli" });
      strict.keys.get(dead)!.revoked = true;
      await new CredentialStore(credPath(home), process.platform, () => {}).set(strict.url, { api_key: dead, expires_at: null, grant_id: null, scopes: [] });
      strict.deviceScript = ["approve"];
      const r = await runCli(["login", "--no-browser"], { home, apiUrl: strict.url });
      assert.equal(r.code, 0, r.stderr);
      const polls = strict.requestsTo("POST", "/api/connectors/device/token");
      assert.deepEqual(polls.map((q) => q.headers.authorization), [`Bearer ${dead}`, undefined]);
      assert.ok(!r.stdout.includes(dead) && !r.stderr.includes(dead));
    } finally {
      await strict.stop();
    }
  });

  it("login --with-token --force revokes the previous stored key itself", async () => {
    const oldKey = mock.issueKey({ channel: "cli" });
    const pasted = mock.issueKey({ channel: "key" });
    await new CredentialStore(credPath(home), process.platform, () => {}).set(mock.url, { api_key: oldKey, expires_at: null, grant_id: null, scopes: [] });
    const r = await runCli(["login", "--with-token", "--force"], { home, apiUrl: mock.url, stdin: pasted });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(mock.keyRecord(oldKey)?.revoked, true);
    assert.match(r.stdout, /Revoked the previous key/);
  });

  it("--force keeps the drafts started with the old key reachable", async () => {
    mock.deviceScript = ["approve"];
    assert.equal((await runCli(["login", "--no-browser"], { home, apiUrl: mock.url })).code, 0);
    const store = new CredentialStore(credPath(home), process.platform, () => {});
    const oldKey = (await store.get(mock.url))!.api_key;
    const started = await runCli(["intake", "start", "--json", "--set", "merchant_name=Example Air"], { home, apiUrl: mock.url });
    const id = started.json().id;

    mock.deviceScript = ["approve"];
    const relogin = await runCli(["login", "--force", "--no-browser", "--json"], { home, apiUrl: mock.url });
    assert.equal(relogin.code, 0, relogin.stderr);
    assert.equal(mock.keyRecord(oldKey)?.revoked, true);
    assert.doesNotMatch(relogin.stdout + relogin.stderr, /no longer be reached/);

    const show = await runCli(["intake", "show", id, "--json"], { home, apiUrl: mock.url });
    assert.equal(show.code, 0, show.stderr);
    const list = await runCli(["intake", "list", "--json"], { home, apiUrl: mock.url });
    assert.deepEqual(list.json().map((d: { id: string }) => d.id), [id]);
  });

  it("refuses a key given as an argument without echoing it", async () => {
    const key = mock.issueKey();
    for (const argv of [["login", key], ["login", "--with-token", key]]) {
      const r = await runCli(argv, { home, apiUrl: mock.url });
      assert.equal(r.code, 2);
      assert.match(r.stderr, /Never put a key on the command line/);
      assert.ok(!r.stderr.includes(key) && !r.stdout.includes(key));
    }
    const r = await runCli(["login", `--token=${key}`], { home, apiUrl: mock.url });
    assert.equal(r.code, 2);
    assert.ok(!r.stderr.includes(key));
  });
});

describe("login --with-token", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;
  before(async () => {
    mock = new MockServer();
    await mock.start();
  });
  after(() => mock.stop());
  beforeEach(async () => ({ home, cleanup } = await tempHome()));
  afterEach(() => cleanup());

  it("reads a piped key, validates it with GET /v1/key and saves it", async () => {
    const key = mock.issueKey({ scopes: ["claims:read"] });
    const r = await runCli(["login", "--with-token"], { home, apiUrl: mock.url, stdin: `${key}\n` });
    assert.equal(r.code, 0, r.stderr);
    // A key made at /connect/muse does not say whose account it is.
    assert.match(r.stdout, /^Signed in \(key expires \d{4}-\d{2}-\d{2}/m);
    assert.ok(!r.stdout.includes(key));
    const saved = JSON.parse(await readFile(credPath(home), "utf8"));
    assert.equal(saved[mock.url].api_key, key);
    assert.deepEqual(saved[mock.url].scopes, ["claims:read"]);
    assert.equal(saved[mock.url].grant_id, mock.keyRecord(key)?.id);
  });

  it("reads from a hidden prompt on a terminal", async () => {
    const key = mock.issueKey();
    const r = await runCli(["login", "--with-token", "--json"], { home, apiUrl: mock.url, stdinIsTTY: true, answers: [key] });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.prompts.length, 1);
    assert.ok(!("account_email" in r.json()), "no account_email for a pasted key");
    assert.ok(!r.stdout.includes(key));
  });

  it("exits 3 and saves nothing when the key is rejected", async () => {
    const key = mock.issueKey();
    mock.keys.get(key)!.revoked = true;
    const r = await runCli(["login", "--with-token"], { home, apiUrl: mock.url, stdin: key });
    assert.equal(r.code, 3);
    assert.ok(!r.stderr.includes(key));
    await assert.rejects(stat(credPath(home)));
  });

  it("rejects text that is not a key", async () => {
    const r = await runCli(["login", "--with-token"], { home, apiUrl: mock.url, stdin: "hello world" });
    assert.equal(r.code, 2);
    assert.match(r.stderr, /does not look like a TourClaim key/);
  });
});

describe("credentials", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;
  before(async () => {
    mock = new MockServer();
    await mock.start();
  });
  after(() => mock.stop());
  beforeEach(async () => ({ home, cleanup } = await tempHome()));
  afterEach(() => cleanup());

  const storeFor = (h: string) => new CredentialStore(credPath(h), process.platform, () => {});

  it("TOURCLAIM_API_KEY takes precedence over the stored key", async () => {
    const stored = mock.issueKey({ traveler: "stored@example.com" });
    const env = mock.issueKey({ traveler: "env@example.com", channel: "cli" });
    await storeFor(home).set(mock.url, { api_key: stored, expires_at: null, grant_id: null, scopes: [] });
    const r = await runCli(["status", "--json"], { home, apiUrl: mock.url, env: { TOURCLAIM_API_KEY: env } });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.json().account_email, "env@example.com");
    assert.equal(r.json().key_source, "env");
    const last = mock.requestsTo("GET", "/api/connectors/v1/key").at(-1);
    assert.equal(last?.headers.authorization, `Bearer ${env}`);
    assert.ok(!r.stdout.includes(env) && !r.stderr.includes(env));
  });

  it("stores keys per API URL", async () => {
    const store = storeFor(home);
    await store.set("https://a.example", { api_key: "tc_muse_a", expires_at: null, grant_id: null, scopes: [] });
    await store.set("https://b.example", { api_key: "tc_muse_b", expires_at: null, grant_id: null, scopes: [] });
    assert.equal((await store.get("https://a.example"))?.api_key, "tc_muse_a");
    assert.equal(await store.remove("https://a.example"), true);
    assert.equal(await store.get("https://a.example"), null);
    assert.equal((await store.get("https://b.example"))?.api_key, "tc_muse_b");
  });

  it("tightens an existing credentials file and directory to 0600/0700", { skip: isWindows }, async () => {
    const dir = dirname(credPath(home));
    await mkdir(dir, { recursive: true });
    await chmod(dir, 0o755);
    await writeFile(join(dir, "credentials.json"), "{}", { mode: 0o644 });
    await storeFor(home).set(mock.url, { api_key: "tc_muse_x", expires_at: null, grant_id: null, scopes: [] });
    assert.equal((await stat(join(dir, "credentials.json"))).mode & 0o777, 0o600);
    assert.equal((await stat(dir)).mode & 0o777, 0o700);
  });

  it("warns when the credentials file is readable by others", { skip: isWindows }, async () => {
    const key = mock.issueKey();
    await storeFor(home).set(mock.url, { api_key: key, expires_at: null, grant_id: null, scopes: [] });
    await chmod(credPath(home), 0o644);
    const r = await runCli(["status"], { home, apiUrl: mock.url });
    assert.match(r.stderr, /can be read by other users/);
  });

  it("warns when the stored key expires within 3 days", async () => {
    const key = mock.issueKey();
    const soon = new Date(Date.now() + 2 * 24 * 3_600_000 + 3_600_000).toISOString().slice(0, 19);
    await storeFor(home).set(mock.url, { api_key: key, expires_at: soon, grant_id: null, scopes: [] });
    const r = await runCli(["claims", "list"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.match(r.stderr, /warning: Your TourClaim key expires in 2 days/);

    const later = new Date(Date.now() + 20 * 24 * 3_600_000).toISOString().slice(0, 19);
    await storeFor(home).set(mock.url, { api_key: key, expires_at: later, grant_id: null, scopes: [] });
    const quiet = await runCli(["claims", "list"], { home, apiUrl: mock.url });
    assert.doesNotMatch(quiet.stderr, /expires/);
  });

  it("uses XDG_CONFIG_HOME, and %APPDATA% on Windows", async () => {
    const { credentialsPath } = await import("../src/credentials.js");
    assert.equal(credentialsPath({ XDG_CONFIG_HOME: "/x/cfg" }, "linux", "/home/p"), "/x/cfg/tourclaim/credentials.json");
    assert.equal(credentialsPath({}, "darwin", "/Users/p"), "/Users/p/.config/tourclaim/credentials.json");
    assert.equal(credentialsPath({ XDG_CONFIG_HOME: "relative" }, "linux", "/home/p"), "/home/p/.config/tourclaim/credentials.json");
    assert.equal(
      credentialsPath({ APPDATA: "C:\\Users\\p\\AppData\\Roaming", XDG_CONFIG_HOME: "/ignored" }, "win32", "C:\\Users\\p"),
      "C:\\Users\\p\\AppData\\Roaming\\tourclaim\\credentials.json",
    );
    assert.equal(credentialsPath({}, "win32", "C:\\Users\\p"), "C:\\Users\\p\\AppData\\Roaming\\tourclaim\\credentials.json");
  });
});

describe("logout and status", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;
  before(async () => {
    mock = new MockServer();
    await mock.start();
  });
  after(() => mock.stop());
  beforeEach(async () => ({ home, cleanup } = await tempHome()));
  afterEach(() => cleanup());

  const save = (key: string) =>
    new CredentialStore(credPath(home), process.platform, () => {}).set(mock.url, {
      api_key: key,
      expires_at: null,
      grant_id: null,
      scopes: [],
    });

  it("logout revokes the key on the server and removes it locally", async () => {
    const key = mock.issueKey();
    await save(key);
    const r = await runCli(["logout", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.deepEqual(r.json(), { api_url: mock.url, revoked: true, already_invalid: false, credential_removed: true, key_source: "stored" });
    assert.equal(mock.keyRecord(key)?.revoked, true);
    await assert.rejects(stat(credPath(home)));
  });

  it("logout removes the stored key even when the server says 401", async () => {
    const key = mock.issueKey();
    mock.keys.get(key)!.revoked = true;
    await save(key);
    const r = await runCli(["logout"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.match(r.stdout, /already expired or revoked/);
    await assert.rejects(stat(credPath(home)));
  });

  it("logout keeps the key when the server cannot be reached", async () => {
    const key = mock.issueKey();
    await save(key);
    mock.enabled = false;
    try {
      const r = await runCli(["logout"], { home, apiUrl: mock.url });
      assert.equal(r.code, 7);
      assert.match(r.stderr, /still stored/);
      await stat(credPath(home));
    } finally {
      mock.enabled = true;
    }
  });

  it("status shows review mode prominently, the account and key expiry", async () => {
    await save(mock.issueKey({ channel: "cli" }));
    const r = await runCli(["status"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.stdout.split("\n")[0], "REVIEW MODE: CLAIMS ARE SYNTHETIC AND NOTHING IS FILED");
    assert.match(r.stdout, /Signed in:\s+yes, as pat@example\.com/);
    assert.match(r.stdout, /Key:\s+[0-9a-f-]{36}, expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/);
    assert.match(r.stdout, new RegExp(`Manage keys:\\s+${mock.url.replace(/\./g, "\\.")}/connect/muse`));
  });

  it("status without an account email for a pasted key", async () => {
    await save(mock.issueKey({ channel: "key" }));
    const human = await runCli(["status"], { home, apiUrl: mock.url });
    assert.match(human.stdout, /^Signed in:\s+yes$/m);
    const json = await runCli(["status", "--json"], { home, apiUrl: mock.url });
    assert.equal(json.json().signed_in, true);
    assert.ok(!("account_email" in json.json()));
  });

  it("whoami is an alias for status; --json carries account_email and mode", async () => {
    await save(mock.issueKey({ channel: "cli" }));
    const r = await runCli(["whoami", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const s = r.json();
    assert.equal(s.mode, "review");
    assert.equal(s.enabled, true);
    assert.equal(s.signed_in, true);
    assert.equal(s.account_email, "pat@example.com");
    assert.equal(s.connector.openapi_url, `${mock.url}/api/connectors/v1/openapi-cli.json`, "the CLI's schema when the server has it");
    assert.equal(s.connector.assistant_openapi_url, `${mock.url}/api/connectors/v1/openapi.json`);
    assert.equal(s.connector.cli_login_url, `${mock.url}/connect/cli`);
  });

  it("status still accepts relative discovery URLs from older servers", async () => {
    const old = new MockServer({ relativeDiscovery: true });
    await old.start();
    try {
      const r = await runCli(["status", "--json"], { home, apiUrl: old.url });
      assert.equal(r.json().connector.connection_url, `${old.url}/connect/muse`);
      assert.equal(r.json().connector.openapi_url, `${old.url}/api/connectors/v1/openapi.json`);
      assert.equal(r.json().connector.cli_login_url, `${old.url}/connect/cli`);
    } finally {
      await old.stop();
    }
  });

  it("status exits 3 when not signed in, and 7 when the connector is disabled", async () => {
    let r = await runCli(["status", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 3);
    assert.equal(r.json().signed_in, false);
    mock.enabled = false;
    try {
      r = await runCli(["status", "--json"], { home, apiUrl: mock.url });
      assert.equal(r.code, 7);
      assert.equal(r.json().enabled, false);
    } finally {
      mock.enabled = true;
    }
  });
});
