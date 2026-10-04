import assert from "node:assert/strict";
import { readFile, stat, writeFile, mkdir, chmod } from "node:fs/promises";
import { join } from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { CredentialStore } from "../src/credentials.js";
import { runCli, tempHome } from "./harness.js";
import { MockServer } from "./mock-server.js";

const isWindows = process.platform === "win32";

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
    mock.rateLimit.remaining = 0;
  });
  afterEach(() => cleanup());

  it("polls until approved, honoring interval and slow_down, and saves a 0600 credential", async () => {
    mock.deviceScript = ["pending", "slow_down", "pending", "approve"];
    const r = await runCli(["login", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    // 5 s, 5 s, then slow_down adds 5 s for every later poll.
    assert.deepEqual(r.sleeps, [5000, 5000, 10000, 10000]);
    assert.match(r.stdout, /Open\s+http:\/\/127\.0\.0\.1:\d+\/connect\/cli/);
    assert.match(r.stdout, /Enter\s+[A-Z]{4}-[A-Z]{4}/);
    assert.match(r.stdout, /Signed in as pat@example\.com \(key expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC, in (29|30) days\)/);
    assert.match(r.stdout, /Review mode: claims are synthetic and nothing is filed/);
    assert.deepEqual(r.opened, [], "--no-browser must not open anything");

    const path = join(home, ".config", "tourclaim", "credentials.json");
    const saved = JSON.parse(await readFile(path, "utf8"));
    const entry = saved[mock.url];
    assert.match(entry.api_key, /^tc_muse_[A-Za-z0-9_-]{48}$/);
    assert.equal(typeof entry.expires_at, "string");
    assert.equal(typeof entry.grant_id, "string");
    assert.deepEqual(entry.scopes, ["intakes:write", "evidence:write", "claims:submit", "claims:read"]);
    if (!isWindows) {
      assert.equal((await stat(path)).mode & 0o777, 0o600);
      assert.equal((await stat(join(home, ".config", "tourclaim"))).mode & 0o777, 0o700);
    }
    assert.ok(!r.stdout.includes(entry.api_key) && !r.stderr.includes(entry.api_key), "the key must never be printed");
  });

  it("sends the client string and requested scopes, and the User-Agent", async () => {
    mock.deviceScript = ["approve"];
    const before = mock.requests.length;
    const r = await runCli(["login", "--no-browser", "--scope", "intakes:write,claims:read"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const codeReq = mock.requestsTo("POST", "/api/connectors/device/code").at(-1);
    assert.deepEqual(codeReq?.body, { client: "tourclaim-cli/0.1.0 (" + (process.platform === "win32" ? "linux" : process.platform) + " x64; node 20.0.0)", scopes: ["intakes:write", "claims:read"] });
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
    assert.equal(first.verification_uri_complete, `${mock.url}/connect/cli?code=${first.user_code}`);
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
    assert.match(r.opened[0] ?? "", /\/connect\/cli\?code=[A-Z]{4}-[A-Z]{4}$/);

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
    assert.equal(await new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {}).get(mock.url), null);
  });

  it("exits 3 when the code expired or was already used", async () => {
    mock.deviceScript = ["expired"];
    const r = await runCli(["login"], { home, apiUrl: mock.url });
    assert.equal(r.code, 3);
    assert.match(r.stderr, /expired or was already used/);
  });

  it("stops polling at expires_in", async () => {
    const short = new MockServer({ deviceInterval: 5, deviceExpiresIn: 12 });
    await short.start();
    try {
      const r = await runCli(["login"], { home, apiUrl: short.url });
      assert.equal(r.code, 3);
      assert.deepEqual(r.sleeps, [5000, 5000]);
      assert.match(r.stderr, /expired before it was approved/);
    } finally {
      await short.stop();
    }
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

  it("reports an existing valid key instead of starting a new sign-in", async () => {
    const key = mock.issueKey();
    await new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {}).set(mock.url, {
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

  it("--force replaces a valid key and revokes the old one", async () => {
    const oldKey = mock.issueKey();
    const store = new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {});
    await store.set(mock.url, { api_key: oldKey, expires_at: null, grant_id: null, scopes: [] });
    mock.deviceScript = ["approve"];
    const r = await runCli(["login", "--force", "--no-browser"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(mock.keyRecord(oldKey)?.revoked, true);
    assert.notEqual((await store.get(mock.url))?.api_key, oldKey);
    assert.match(r.stdout, /Revoked the previous key/);
  });

  it("--force keeps the drafts started with the old key reachable", async () => {
    mock.deviceScript = ["approve"];
    assert.equal((await runCli(["login", "--no-browser"], { home, apiUrl: mock.url })).code, 0);
    const store = new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {});
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
    assert.match(r.stdout, /Signed in as pat@example\.com \(key expires/);
    assert.ok(!r.stdout.includes(key));
    const saved = JSON.parse(await readFile(join(home, ".config", "tourclaim", "credentials.json"), "utf8"));
    assert.equal(saved[mock.url].api_key, key);
    assert.deepEqual(saved[mock.url].scopes, ["claims:read"]);
    assert.equal(saved[mock.url].grant_id, mock.keyRecord(key)?.id);
  });

  it("reads from a hidden prompt on a terminal", async () => {
    const key = mock.issueKey();
    const r = await runCli(["login", "--with-token", "--json"], { home, apiUrl: mock.url, stdinIsTTY: true, answers: [key] });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.prompts.length, 1);
    assert.equal(r.json().account_email, "pat@example.com");
    assert.ok(!r.stdout.includes(key));
  });

  it("exits 3 and saves nothing when the key is rejected", async () => {
    const key = mock.issueKey();
    mock.keys.get(key)!.revoked = true;
    const r = await runCli(["login", "--with-token"], { home, apiUrl: mock.url, stdin: key });
    assert.equal(r.code, 3);
    assert.ok(!r.stderr.includes(key));
    await assert.rejects(stat(join(home, ".config", "tourclaim", "credentials.json")));
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

  const storeFor = (h: string) => new CredentialStore(join(h, ".config", "tourclaim", "credentials.json"), process.platform, () => {});

  it("TOURCLAIM_API_KEY takes precedence over the stored key", async () => {
    const stored = mock.issueKey({ traveler: "stored@example.com" });
    const env = mock.issueKey({ traveler: "env@example.com" });
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
    const dir = join(home, ".config", "tourclaim");
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
    await chmod(join(home, ".config", "tourclaim", "credentials.json"), 0o644);
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
    assert.equal(credentialsPath({ XDG_CONFIG_HOME: "/x/cfg" }, "linux", "/home/p"), join("/x/cfg", "tourclaim", "credentials.json"));
    assert.equal(credentialsPath({}, "darwin", "/Users/p"), join("/Users/p", ".config", "tourclaim", "credentials.json"));
    assert.equal(credentialsPath({ XDG_CONFIG_HOME: "relative" }, "linux", "/home/p"), join("/home/p", ".config", "tourclaim", "credentials.json"));
    assert.ok(credentialsPath({ APPDATA: "C:\\Users\\p\\AppData\\Roaming" }, "win32", "C:\\Users\\p").includes("tourclaim"));
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
    new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {}).set(mock.url, {
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
    await assert.rejects(stat(join(home, ".config", "tourclaim", "credentials.json")));
  });

  it("logout removes the stored key even when the server says 401", async () => {
    const key = mock.issueKey();
    mock.keys.get(key)!.revoked = true;
    await save(key);
    const r = await runCli(["logout"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.match(r.stdout, /already expired or revoked/);
    await assert.rejects(stat(join(home, ".config", "tourclaim", "credentials.json")));
  });

  it("logout keeps the key when the server cannot be reached", async () => {
    const key = mock.issueKey();
    await save(key);
    mock.enabled = false;
    try {
      const r = await runCli(["logout"], { home, apiUrl: mock.url });
      assert.equal(r.code, 7);
      assert.match(r.stderr, /still stored/);
      await stat(join(home, ".config", "tourclaim", "credentials.json"));
    } finally {
      mock.enabled = true;
    }
  });

  it("status shows review mode prominently, the account and key expiry", async () => {
    await save(mock.issueKey());
    const r = await runCli(["status"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    assert.equal(r.stdout.split("\n")[0], "REVIEW MODE: CLAIMS ARE SYNTHETIC AND NOTHING IS FILED");
    assert.match(r.stdout, /Signed in:\s+yes, as pat@example\.com/);
    assert.match(r.stdout, /Key:\s+[0-9a-f-]{36}, expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/);
    assert.match(r.stdout, new RegExp(`Manage keys:\\s+${mock.url.replace(/\./g, "\\.")}/connect/muse`));
  });

  it("whoami is an alias for status; --json carries account_email and mode", async () => {
    await save(mock.issueKey());
    const r = await runCli(["whoami", "--json"], { home, apiUrl: mock.url });
    assert.equal(r.code, 0, r.stderr);
    const s = r.json();
    assert.equal(s.mode, "review");
    assert.equal(s.enabled, true);
    assert.equal(s.signed_in, true);
    assert.equal(s.account_email, "pat@example.com");
    assert.equal(s.connector.openapi_url, `${mock.url}/api/connectors/v1/openapi.json`);
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
