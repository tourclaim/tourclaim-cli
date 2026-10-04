import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { after, afterEach, before, beforeEach, describe, it } from "node:test";
import { CredentialStore } from "../src/credentials.js";
import { runCli, tempHome, type CliOptions, type CliResult } from "./harness.js";
import { MockServer } from "./mock-server.js";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1EAAAAASUVORK5CYII=", "base64");
const PDF = Buffer.from("%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n");

const COMPLETE = [
  "merchant_name=Example Air",
  "booking_ref=EXA-482913",
  "trip_date=2026-03-04",
  "booking_amount=250.00",
  "refunded_amount=50.00",
  "currency=usd",
  "card_product_id=412",
  "reason_category=weather",
  "narrative=The harbor closed for a storm warning and the tour was cancelled.",
  "other_insurance=no",
];

describe("intake and claims", () => {
  let mock: MockServer;
  let home: string;
  let cleanup: () => Promise<void>;
  let key: string;
  let traveler: string;
  let travelerCount = 0;

  before(async () => {
    mock = new MockServer();
    await mock.start();
  });
  after(() => mock.stop());
  beforeEach(async () => {
    ({ home, cleanup } = await tempHome());
    // A traveler per test: claims and one-claim-per-booking are per traveler.
    traveler = `traveler${++travelerCount}@example.com`;
    // A key as tourclaim login (device flow) mints it.
    key = mock.issueKey({ traveler, channel: "cli" });
    await new CredentialStore(join(home, ".config", "tourclaim", "credentials.json"), process.platform, () => {}).set(mock.url, {
      api_key: key,
      expires_at: null,
      grant_id: null,
      scopes: [],
    });
    mock.rateLimit = { remaining: 0, retryAfter: "1" };
    mock.inject = [];
    mock.onRequest = null;
    mock.enabled = true;
  });
  afterEach(() => cleanup());

  /** Runs the CLI and checks the key never appears in its output. */
  async function cli(argv: string[], options: Partial<CliOptions> = {}): Promise<CliResult> {
    const r = await runCli(argv, { home, apiUrl: mock.url, ...options });
    for (const secret of [key, ...[...mock.keys.keys()]]) {
      assert.ok(!r.stdout.includes(secret), `key printed on stdout by: ${argv.join(" ")}`);
      assert.ok(!r.stderr.includes(secret), `key printed on stderr by: ${argv.join(" ")}`);
    }
    return r;
  }

  async function start(pairs: string[] = COMPLETE): Promise<string> {
    const r = await cli(["intake", "start", "--json", ...pairs.flatMap((p) => ["--set", p])]);
    assert.equal(r.code, 0, r.stderr);
    return r.json().id as string;
  }

  describe("start", () => {
    it("generates a UUID Idempotency-Key and prints what is missing", async () => {
      const r = await cli(["intake", "start", "--set", "merchant_name=Example Air", "--set", "reason_category=airline_cancellation"]);
      assert.equal(r.code, 0, r.stderr);
      const req = mock.requestsTo("POST", "/api/connectors/v1/intakes").at(-1);
      assert.match(String(req?.headers["idempotency-key"]), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.deepEqual(req?.body, { fields: { merchant_name: "Example Air", reason_category: "AIRLINE_CANCELLATION" } });
      assert.match(r.stdout, /^Started draft [0-9a-f-]{36}/);
      assert.match(r.stdout, /State:\s+collecting/);
      assert.match(r.stdout, /Mode:\s+review mode: claims are synthetic and nothing is filed/);
      assert.match(r.stdout, /Missing \(8\): booking_ref, trip_date/);
      assert.match(r.stdout, /1\. What is the booking confirmation number\?/);
    });

    it("prints the API's IntakeResponse unchanged with --json", async () => {
      const r = await cli(["intake", "start", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      const intake = r.json();
      assert.equal(intake.state, "collecting");
      assert.equal(intake.revision, 1);
      assert.equal(intake.coverage_status, "not_determined");
      assert.equal(r.jsonLines().length, 1);
    });

    it("returns the same draft for the same key and body, and exits 4 for a different body", async () => {
      const args = ["intake", "start", "--json", "--idempotency-key", "retry-key-0001", "--set", "booking_ref=ABC-1"];
      const first = await cli(args);
      const again = await cli(args);
      assert.equal(first.json().id, again.json().id);
      const different = await cli(["intake", "start", "--json", "--idempotency-key", "retry-key-0001", "--set", "booking_ref=ABC-2"]);
      assert.equal(different.code, 4);
      assert.equal(different.jsonError().code, "idempotency_key_reused");
      assert.equal(different.jsonError().idempotency_key, "retry-key-0001");
      assert.match(different.jsonError().message, /new --idempotency-key/);
    });

    it("on a concurrent-request 409, says to retry with the same key rather than a new one", async () => {
      mock.inject.push({ status: 409, body: { detail: "Concurrent request; retry with the same idempotency key" } });
      const r = await cli(["intake", "start", "--json", "--idempotency-key", "concurrent-0001"]);
      assert.equal(r.code, 4);
      assert.match(r.jsonError().message, /Retry with the same fields and --idempotency-key concurrent-0001/);
      assert.doesNotMatch(r.jsonError().message, /new --idempotency-key/);
    });

    it("rejects a malformed --idempotency-key before calling the API", async () => {
      const before = mock.requests.length;
      const r = await cli(["intake", "start", "--idempotency-key", "short"]);
      assert.equal(r.code, 2);
      assert.equal(mock.requests.length, before);
    });

    it("merges --fields-file with --set and rejects unknown fields", async () => {
      const file = join(home, "fields.json");
      await writeFile(file, JSON.stringify({ merchant_name: "From File", booking_ref: "F-1", medical: { provider_seen: true } }));
      const r = await cli(["intake", "start", "--json", "--fields-file", file, "--set", "booking_ref=F-2"]);
      assert.equal(r.code, 0, r.stderr);
      assert.equal(r.json().fields.merchant_name, "From File");
      assert.equal(r.json().fields.booking_ref, "F-2");
      await writeFile(file, JSON.stringify({ card_number: "4111" }));
      const bad = await cli(["intake", "start", "--fields-file", file]);
      assert.equal(bad.code, 2);
      assert.match(bad.stderr, /Unknown field "card_number"/);
    });

    it("tells the user to retry with the same key when the outcome is unknown", async () => {
      mock.rateLimit = { remaining: 2, retryAfter: "1" };
      const r = await cli(["intake", "start", "--json", "--idempotency-key", "unknown-outcome-1"]);
      assert.equal(r.code, 5);
      assert.match(r.jsonError().message, /--idempotency-key unknown-outcome-1/);
    });
  });

  describe("set", () => {
    it("types values by the schema, clears with null and passes := JSON through", async () => {
      const id = await start(["merchant_name=Example Air"]);
      const r = await cli([
        "intake",
        "set",
        id,
        "card_product_id=412",
        "reason_category=illness",
        "booking_amount=250.00",
        "medical.provider_seen=yes",
        "medical.symptom_onset_date=2026-03-01",
        "other_insurance:=\"UNSURE\"",
        "--clear",
        "merchant_name",
        "--json",
      ]);
      assert.equal(r.code, 0, r.stderr);
      const patch = mock.requestsTo("PATCH", `/api/connectors/v1/intakes/${id}`).at(-1);
      assert.deepEqual(patch?.body, {
        expected_revision: 1,
        fields: {
          card_product_id: 412,
          reason_category: "ILLNESS",
          booking_amount: "250.00",
          medical: { provider_seen: true, symptom_onset_date: "2026-03-01" },
          other_insurance: "UNSURE",
          merchant_name: null,
        },
      });
      const intake = r.json();
      assert.equal(intake.revision, 2);
      assert.equal(intake.fields.merchant_name, null);
      assert.ok(intake.missing_fields.includes("medical.prior_condition"));
    });

    it("merges medical answers and reads the revision first", async () => {
      const id = await start(["reason_category=injury", "medical.provider_seen=true"]);
      const r = await cli(["intake", "set", id, "medical.prior_condition=false", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.json().fields.medical, { provider_seen: true, prior_condition: false });
      assert.equal(mock.requestsTo("GET", `/api/connectors/v1/intakes/${id}`).length, 1);
    });

    it("on 409 re-reads the draft, explains, exits 4 and does not retry", async () => {
      const id = await start(["merchant_name=Example Air"]);
      mock.onRequest = (req) => {
        if (req.method === "PATCH") {
          const rec = mock.intakes.get(id);
          if (rec) rec.revision += 1;
        }
      };
      const r = await cli(["intake", "set", id, "booking_ref=X-1", "--json"]);
      assert.equal(r.code, 4);
      const err = r.jsonError();
      assert.equal(err.code, "stale_revision");
      assert.equal(err.current_revision, 2);
      assert.match(err.message, /now at revision 2 .*used revision 1.*Nothing was saved/);
      assert.equal(mock.requestsTo("PATCH", `/api/connectors/v1/intakes/${id}`).length, 1);
    });

    it("rejects unknown fields, empty values and empty changes as usage errors", async () => {
      const id = await start([]);
      for (const argv of [
        ["intake", "set", id, "card_number=4111111111111111"],
        ["intake", "set", id, "narrative="],
        ["intake", "set", id],
        ["intake", "set", id, "trip_date=March 4"],
        ["intake", "set", id, "booking_amount=$250"],
        ["intake", "set", id, "reason_category=bored"],
        ["intake", "set", id, "medical=yes"],
        ["intake", "set", id, "merchant_name=A", "--clear", "merchant_name"],
      ]) {
        const r = await cli(argv);
        assert.equal(r.code, 2, `${argv.join(" ")}: ${r.stderr}`);
      }
      assert.equal(mock.requestsTo("PATCH", `/api/connectors/v1/intakes/${id}`).length, 0);
    });

    it("reports a 422 validation list with each field (exit 1)", async () => {
      const id = await start([]);
      const human = await cli(["intake", "set", id, "narrative=too short"]);
      assert.equal(human.code, 1);
      assert.match(human.stderr, /error: The API rejected these values: fields\.narrative \(string_too_short\)/);
      assert.match(human.stderr, /fields\.narrative: Invalid field value \(string_too_short\)/);
      const json = await cli(["intake", "set", id, "narrative=too short", "--json"]);
      assert.equal(json.code, 1);
      assert.deepEqual(json.jsonError().detail, [{ loc: ["body", "fields", "narrative"], type: "string_too_short", msg: "Invalid field value" }]);
      assert.equal(json.jsonError().status, 422);
    });

    it("reports a 422 string detail (exit 1)", async () => {
      const id = await start(["booking_amount=100.00"]);
      const r = await cli(["intake", "set", id, "refunded_amount=200.00"]);
      assert.equal(r.code, 1);
      assert.match(r.stderr, /refund cannot exceed amount paid/);
    });

    it("warns that a change cancels the traveler's signature", async () => {
      const id = await start();
      mock.sign(id);
      const r = await cli(["intake", "set", id, "cancellation_policy=Non-refundable within 48 hours."]);
      assert.equal(r.code, 0, r.stderr);
      assert.match(r.stderr, /cancelled the traveler's signature/);
      assert.match(r.stdout, /State:\s+needs_approval/);
    });
  });

  describe("errors", () => {
    it("404 explains that drafts belong to the key that created them (exit 6)", async () => {
      const id = await start([]);
      const other = mock.issueKey({ traveler });
      const r = await cli(["intake", "show", id, "--json"], { env: { TOURCLAIM_API_KEY: other } });
      assert.equal(r.code, 6);
      assert.equal(r.jsonError().code, "not_found");
      assert.match(r.jsonError().message, /reachable only with the key that started them/);
      assert.match(r.jsonError().message, /tourclaim intake list/);
    });

    it("401 exits 3; no key at all exits 3 without a request", async () => {
      mock.keys.get(key)!.revoked = true;
      let r = await cli(["claims", "list", "--json"]);
      assert.equal(r.code, 3);
      assert.equal(r.jsonError().code, "unauthorized");
      assert.match(r.jsonError().message, /tourclaim login/);

      const empty = await tempHome();
      const before = mock.requests.length;
      r = await runCli(["claims", "list"], { home: empty.home, apiUrl: mock.url });
      assert.equal(r.code, 3);
      assert.match(r.stderr, /Not signed in/);
      assert.equal(mock.requests.length, before);
      await empty.cleanup();
    });

    it("403 for a missing permission exits 3", async () => {
      const readOnly = mock.issueKey({ traveler, scopes: ["claims:read"] });
      const r = await cli(["cards", "search", "sapphire", "--json"], { env: { TOURCLAIM_API_KEY: readOnly } });
      assert.equal(r.code, 3);
      assert.equal(r.jsonError().code, "forbidden");
    });

    it("429 is retried once after Retry-After, then exits 5", async () => {
      mock.rateLimit = { remaining: 1, retryAfter: "2" };
      let r = await cli(["claims", "list", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.sleeps, [2000]);
      assert.match(r.stderr, /retrying in 2 seconds/);

      mock.rateLimit = { remaining: 2, retryAfter: "2" };
      r = await cli(["claims", "list", "--json"]);
      assert.equal(r.code, 5);
      assert.deepEqual(r.sleeps, [2000]);
      assert.equal(r.jsonError().code, "rate_limited");
      assert.equal(r.jsonError().retry_after, 2);
      assert.match(r.jsonError().message, /Rate limit exceeded/);
    });

    it("429 asking for more than 60 seconds is not retried", async () => {
      mock.rateLimit = { remaining: 1, retryAfter: "120" };
      const r = await cli(["claims", "list", "--json"]);
      assert.equal(r.code, 5);
      assert.deepEqual(r.sleeps, []);
      assert.equal(r.jsonError().retry_after, 120);
    });

    it("503 exits 7", async () => {
      mock.enabled = false;
      const r = await cli(["claims", "list"]);
      assert.equal(r.code, 7);
      assert.match(r.stderr, /not enabled/);
    });

    it("413 maps to a too_large error", async () => {
      const { ApiClient } = await import("../src/api.js");
      const client = new ApiClient({
        baseUrl: mock.url,
        apiKey: key,
        userAgent: "test",
        fetch,
        sleep: async () => {},
        notice: () => {},
        missingKey: () => new Error("no key") as never,
      });
      await assert.rejects(
        client.request("/api/connectors/v1/intakes/x/attachments", { method: "POST", body: { pad: "x".repeat(9 * 1024 * 1024) } }),
        (error: any) => error.status === 413 && error.code === "too_large" && error.exitCode === 1,
      );
    });

    it("usage errors exit 2", async () => {
      for (const argv of [["intake", "show"], ["intake", "frobnicate"], ["claims", "list", "--offset", "-1"], ["claims", "list", "--bogus"]]) {
        const r = await cli(argv);
        assert.equal(r.code, 2, argv.join(" "));
      }
    });

    it("refuses plain http to a non-local API URL", async () => {
      const r = await cli(["status"], { apiUrl: "http://api.example.com" });
      assert.equal(r.code, 2);
      assert.match(r.stderr, /plain http/);
    });
  });

  describe("attach", () => {
    it("refuses without --yes when there is no terminal, and uploads nothing", async () => {
      const id = await start([]);
      const file = join(home, "receipt.png");
      await writeFile(file, PNG);
      const r = await cli(["intake", "attach", id, file, "--type", "receipt", "--json"]);
      assert.equal(r.code, 2);
      assert.match(r.jsonError().message, /Pass --yes only after the traveler has agreed/);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).length, 0);
    });

    it("asks on a terminal; no means nothing is uploaded", async () => {
      const id = await start([]);
      const file = join(home, "receipt.png");
      await writeFile(file, PNG);
      const no = await cli(["intake", "attach", id, file, "--type", "receipt"], { stdinIsTTY: true, answers: ["n"] });
      assert.equal(no.code, 1);
      assert.match(no.prompts[0] ?? "", /Share receipt\.png \(image\/png, 68 B\) with TourClaim as receipt evidence/);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).length, 0);
      const yes = await cli(["intake", "attach", id, file, "--type", "receipt"], { stdinIsTTY: true, answers: ["yes"] });
      assert.equal(yes.code, 0, yes.stderr);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).length, 1);
    });

    it("uploads with --yes, detecting the type from the bytes", async () => {
      const id = await start([]);
      const file = join(home, "itinerary.dat");
      await writeFile(file, PDF);
      const r = await cli(["intake", "attach", id, file, "--type", "itinerary", "--yes", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      const req = mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).at(-1);
      const body = req?.body as Record<string, unknown>;
      assert.equal(body.content_type, "application/pdf");
      assert.equal(body.filename, "itinerary.dat");
      assert.equal(body.doc_type, "itinerary");
      assert.equal(body.user_authorized_sharing, true);
      assert.equal(body.expected_revision, 1);
      assert.equal(Buffer.from(body.content_base64 as string, "base64").toString(), PDF.toString());
      assert.equal(r.json().evidence[0].label, "File evidence 1");
      assert.equal(r.json().revision, 2);
    });

    it("rejects unsupported, oversized and missing files, and bad --type", async () => {
      const id = await start([]);
      const text = join(home, "notes.pdf");
      await writeFile(text, "just text pretending to be a pdf");
      let r = await cli(["intake", "attach", id, text, "--type", "other", "--yes"]);
      assert.equal(r.code, 1);
      assert.match(r.stderr, /not a PDF, JPEG or PNG/);

      const big = join(home, "big.png");
      await writeFile(big, Buffer.concat([PNG, Buffer.alloc(5 * 1024 * 1024)]));
      r = await cli(["intake", "attach", id, big, "--type", "receipt", "--yes"]);
      assert.equal(r.code, 1);
      assert.match(r.stderr, /the limit is 5 MiB/);

      r = await cli(["intake", "attach", id, join(home, "missing.png"), "--type", "receipt", "--yes"]);
      assert.equal(r.code, 1);
      r = await cli(["intake", "attach", id, text, "--yes"]);
      assert.equal(r.code, 2);
      r = await cli(["intake", "attach", id, text, "--type", "photo", "--yes"]);
      assert.equal(r.code, 2);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/attachments`).length, 0);
    });
  });

  describe("add-email", () => {
    const EML = [
      "From: \"Example Air\" <bookings@example-air.test>",
      "To: pat@example.com",
      "Subject: =?UTF-8?B?WW91ciBib29raW5nIEVYQS00ODI5MTMgaGFzIGJlZW4gY2FuY2VsbGVk?=",
      "Date: Tue, 3 Mar 2026 14:05:00 +0000",
      "Message-ID: <18e4c2a9f1b7d035@example-air.test>",
      "MIME-Version: 1.0",
      "Content-Type: multipart/alternative; boundary=\"b1\"",
      "",
      "--b1",
      "Content-Type: text/plain; charset=utf-8",
      "Content-Transfer-Encoding: quoted-printable",
      "",
      "We're sorry: flight EX 204 on 4 March has been cancelled. Caf=C3=A9 vouchers =",
      "are available.",
      "--b1",
      "Content-Type: text/html",
      "",
      "<p>ignored</p>",
      "--b1--",
      "",
    ].join("\r\n");

    it("parses an .eml and sends the message unmodified", async () => {
      const id = await start([]);
      const file = join(home, "cancel.eml");
      await writeFile(file, EML);
      const r = await cli(["intake", "add-email", id, "--eml", file, "--yes", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      const body = mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/email-evidence`).at(-1)?.body;
      assert.deepEqual(body, {
        expected_revision: 1,
        provider: "user",
        message_id: "18e4c2a9f1b7d035@example-air.test",
        subject: "Your booking EXA-482913 has been cancelled",
        sender: "\"Example Air\" <bookings@example-air.test>",
        sent_at: "2026-03-03T14:05:00.000Z",
        text: "We're sorry: flight EX 204 on 4 March has been cancelled. Café vouchers are available.",
        user_authorized_sharing: true,
      });
      assert.equal(r.json().evidence[0].label, "Email evidence 1");
    });

    it("needs --subject and --from with --text-file, and a consent", async () => {
      const id = await start([]);
      const file = join(home, "notice.txt");
      await writeFile(file, "Your tour on 4 March was cancelled because of the storm.");
      let r = await cli(["intake", "add-email", id, "--text-file", file, "--yes"]);
      assert.equal(r.code, 2);
      r = await cli(["intake", "add-email", id, "--text-file", file, "--subject", "Cancelled", "--from", "ops@tours.test"]);
      assert.equal(r.code, 2);
      assert.match(r.stderr, /seen which message will be shared/);
      r = await cli(["intake", "add-email", id, "--text-file", file, "--subject", "Cancelled", "--from", "ops@tours.test", "--sent-at", "2026-03-03 14:05", "--yes"]);
      assert.equal(r.code, 2);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/email-evidence`).length, 0);
    });

    it("derives a stable message id, so adding the same message twice changes nothing", async () => {
      const id = await start([]);
      const file = join(home, "notice.txt");
      await writeFile(file, "Your tour on 4 March was cancelled because of the storm.");
      const args = ["intake", "add-email", id, "--text-file", file, "--subject", "Cancelled", "--from", "ops@tours.test", "--yes", "--json"];
      const first = await cli(args);
      const second = await cli(args);
      assert.equal(first.code, 0, first.stderr);
      assert.equal(second.code, 0, second.stderr);
      assert.equal(second.json().revision, first.json().revision);
      assert.equal(second.json().evidence.length, 1);
      const ids = mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/email-evidence`).map((r) => (r.body as Record<string, unknown>).message_id);
      assert.equal(ids[0], ids[1]);
      assert.match(String(ids[0]), /^sha256-[0-9a-f]{40}$/);
    });
  });

  describe("sign", () => {
    it("explains what is missing when the draft is incomplete", async () => {
      const id = await start(["merchant_name=Example Air"]);
      const r = await cli(["intake", "sign", id, "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "intake_incomplete");
      assert.ok(r.jsonError().missing_fields.includes("booking_ref"));
    });

    it("prints the review link and opens it only on a terminal", async () => {
      const id = await start();
      let r = await cli(["intake", "sign", id]);
      assert.equal(r.code, 0, r.stderr);
      assert.match(r.stdout, /Only the traveler can sign/);
      assert.match(r.stdout, new RegExp(`Review and sign: ${mock.url.replace(/\./g, "\\.")}/connect/muse\\?intake=${id}`));
      assert.deepEqual(r.opened, []);
      r = await cli(["intake", "sign", id], { stdoutIsTTY: true });
      assert.deepEqual(r.opened, [`${mock.url}/connect/muse?intake=${id}`]);
      r = await cli(["intake", "sign", id, "--no-browser"], { stdoutIsTTY: true });
      assert.deepEqual(r.opened, []);
    });

    it("--wait polls every 5 seconds until the traveler signs", async () => {
      const id = await start();
      let polls = 0;
      const r = await cli(["intake", "sign", id, "--wait", "--json"], {
        onSleep: () => {
          polls++;
          if (polls === 2) mock.sign(id);
        },
      });
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.sleeps, [5000, 5000]);
      const [waiting, final] = r.jsonLines();
      assert.equal(waiting.event, "waiting_for_signature");
      assert.equal(waiting.review_url, `${mock.url}/connect/muse?intake=${id}`);
      assert.equal(waiting.timeout_seconds, 900);
      assert.equal(final.state, "ready_to_submit");
    });

    it("--wait gives up at --timeout", async () => {
      const id = await start();
      const r = await cli(["intake", "sign", id, "--wait", "--timeout", "12", "--json"]);
      assert.equal(r.code, 1);
      assert.deepEqual(r.sleeps, [5000, 5000]);
      assert.equal(r.jsonError().code, "timeout");
    });

    it("says so when the draft is already signed", async () => {
      const id = await start();
      mock.sign(id);
      const r = await cli(["intake", "sign", id]);
      assert.equal(r.code, 0);
      assert.match(r.stdout, /already signed/);
      assert.match(r.stdout, new RegExp(`tourclaim intake submit ${id}`));
    });
  });

  describe("submit and claims", () => {
    it("refuses an unsigned draft locally with exit 4", async () => {
      const id = await start();
      const r = await cli(["intake", "submit", id, "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "approval_required");
      assert.equal(r.jsonError().review_url, `${mock.url}/connect/muse?intake=${id}`);
      assert.equal(mock.requestsTo("POST", `/api/connectors/v1/intakes/${id}/submit`).length, 0);
    });

    it("submits a signed draft; retrying returns the same claim", async () => {
      const id = await start();
      mock.sign(id);
      const r = await cli(["intake", "submit", id]);
      assert.equal(r.code, 0, r.stderr);
      assert.match(r.stdout, /Submitted draft .* as claim [0-9a-f-]{36}\./);
      assert.match(r.stdout, /Status:\s+INTAKE_RECEIVED/);
      assert.match(r.stdout, /review mode: claims are synthetic and nothing is filed/);
      assert.match(r.stdout, /Nothing here decides coverage or promises reimbursement/);
      const again = await cli(["intake", "submit", id, "--json"]);
      assert.equal(again.code, 0, again.stderr);
      const claimId = again.json().id;
      assert.equal(mock.intakes.get(id)?.claimId, claimId);

      const list = await cli(["claims", "list", "--json"]);
      assert.deepEqual(list.json().map((c: { id: string }) => c.id), [claimId]);
      const show = await cli(["claims", "show", claimId, "--json"]);
      assert.equal(show.json().status, "INTAKE_RECEIVED");
      const human = await cli(["claims", "show", claimId]);
      assert.match(human.stdout, /Next:\s+Synthetic review claim received/);
    });

    it("exits 4 when the booking already has a claim", async () => {
      const first = await start();
      mock.sign(first);
      assert.equal((await cli(["intake", "submit", first])).code, 0);
      const second = await start([...COMPLETE.slice(0, -1), "other_insurance=unsure"]);
      mock.sign(second);
      const r = await cli(["intake", "submit", second, "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "duplicate_booking");
      assert.match(r.jsonError().message, /already exists/);
    });

    it("lists claims a page at a time and points at the next page", async () => {
      for (let i = 0; i < 31; i++) {
        mock.claims.set(`claim-${i}`, {
          id: `claim-${i}`,
          intakeId: `intake-${i}`,
          traveler,
          bookingKey: `k${i}`,
          status: "INTAKE_RECEIVED",
          nextAction: "Copernican is reviewing your claim and evidence.",
          updatedAt: "2026-03-05T17:20:11Z",
          seq: 1000 + i,
        });
      }
      try {
        const page = await cli(["claims", "list"]);
        assert.match(page.stdout, /More claims may exist: tourclaim claims list --offset 30/);
        const next = await cli(["claims", "list", "--offset", "30", "--json"]);
        assert.equal(next.json().length, 1);
        assert.equal(mock.requestsTo("GET", "/api/connectors/v1/claims").at(-1)?.query.get("offset"), "30");
      } finally {
        for (let i = 0; i < 31; i++) mock.claims.delete(`claim-${i}`);
      }
    });

    it("claims show exits 6 for an unknown claim", async () => {
      const r = await cli(["claims", "show", "nope"]);
      assert.equal(r.code, 6);
    });
  });

  describe("list", () => {
    it("lists open drafts, most recently changed first, without submitted ones", async () => {
      const first = await start(["merchant_name=First Tours"]);
      const second = await start(["merchant_name=Second Air", "booking_ref=SA-1"]);
      const submitted = await start();
      mock.sign(submitted);
      assert.equal((await cli(["intake", "submit", submitted])).code, 0);
      assert.equal((await cli(["intake", "set", first, "trip_date=2026-03-04"])).code, 0);

      const json = await cli(["intake", "list", "--json"]);
      assert.equal(json.code, 0, json.stderr);
      assert.deepEqual(json.json().map((d: { id: string }) => d.id), [first, second]);
      assert.equal(json.json()[0].fields.trip_date, "2026-03-04");

      const human = await cli(["intake", "list"]);
      assert.match(human.stdout, /^DRAFT\s+STATE\s+MERCHANT\s+BOOKING\s+MISSING/);
      assert.match(human.stdout, new RegExp(`${first}\\s+collecting\\s+First Tours\\s+-\\s+8`));
      assert.match(human.stdout, new RegExp(`${second}\\s+collecting\\s+Second Air\\s+SA-1\\s+8`));
      assert.doesNotMatch(human.stdout, new RegExp(submitted));
    });

    it("says when there are none", async () => {
      const r = await cli(["intake", "list"]);
      assert.equal(r.code, 0);
      assert.match(r.stdout, /No open drafts\. Start one with: tourclaim intake start/);
      assert.deepEqual((await cli(["intake", "list", "--json"])).json(), []);
    });

    it("a new tourclaim login key reaches drafts from earlier login keys, even revoked ones", async () => {
      const id = await start(["merchant_name=Before Relogin"]);
      mock.keys.get(key)!.revoked = true;
      const next = mock.issueKey({ traveler, channel: "cli" });
      const list = await cli(["intake", "list", "--json"], { env: { TOURCLAIM_API_KEY: next } });
      assert.equal(list.code, 0, list.stderr);
      assert.deepEqual(list.json().map((d: { id: string }) => d.id), [id]);
      const set = await cli(["intake", "set", id, "booking_ref=AFTER-1", "--json"], { env: { TOURCLAIM_API_KEY: next } });
      assert.equal(set.code, 0, set.stderr);
    });

    it("drafts from other apps' keys and CLI drafts do not see each other", async () => {
      const cliDraft = await start(["merchant_name=From CLI"]);
      const other = mock.issueKey({ traveler, channel: "key" });
      const otherDraft = await cli(["intake", "start", "--json", "--set", "merchant_name=From Muse"], { env: { TOURCLAIM_API_KEY: other } });
      const otherId = otherDraft.json().id;
      const fromOther = await cli(["intake", "list", "--json"], { env: { TOURCLAIM_API_KEY: other } });
      assert.deepEqual(fromOther.json().map((d: { id: string }) => d.id), [otherId]);
      const fromCli = await cli(["intake", "list", "--json"]);
      assert.deepEqual(fromCli.json().map((d: { id: string }) => d.id), [cliDraft]);
      const show = await cli(["intake", "show", otherId]);
      assert.equal(show.code, 6);
      const stranger = mock.issueKey({ traveler: "someone-else@example.com", channel: "cli" });
      const none = await cli(["intake", "list", "--json"], { env: { TOURCLAIM_API_KEY: stranger } });
      assert.deepEqual(none.json(), []);
    });

    it("pages 30 at a time and points at the next page", async () => {
      for (let i = 0; i < 31; i++) await start([`booking_ref=PAGE-${i}`]);
      const page = await cli(["intake", "list"]);
      assert.match(page.stdout, /More drafts may exist: tourclaim intake list --offset 30/);
      const next = await cli(["intake", "list", "--offset", "30", "--json"]);
      assert.equal(next.json().length, 1);
      assert.equal(mock.requestsTo("GET", "/api/connectors/v1/intakes").at(-1)?.query.get("offset"), "30");
    });
  });

  describe("409 causes", () => {
    it("uses the X-TourClaim-Error code as the JSON error code", async () => {
      const id = await start();
      let r = await cli(["intake", "set", id, "booking_ref=X-2", "--revision", "9", "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "stale_revision");
      assert.equal(r.jsonError().current_revision, 1);

      r = await cli(["intake", "submit", id, "--revision", "9", "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "approval_required");
      assert.match(r.jsonError().message, /has not signed revision 1/);
      assert.equal(r.jsonError().review_url, `${mock.url}/connect/muse?intake=${id}`);
    });

    it("evidence_conflict: the same file again under another type", async () => {
      const id = await start([]);
      const file = join(home, "receipt.png");
      await writeFile(file, PNG);
      assert.equal((await cli(["intake", "attach", id, file, "--type", "receipt", "--yes"])).code, 0);
      const r = await cli(["intake", "attach", id, file, "--type", "other", "--yes", "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "evidence_conflict");
      assert.match(r.jsonError().message, /already has that evidence with different details/);
    });

    it("approval_outdated asks the traveler to sign again", async () => {
      const id = await start();
      mock.sign(id);
      mock.intakes.get(id)!.approvalOutdated = true;
      const r = await cli(["intake", "submit", id, "--revision", "1", "--json"]);
      assert.equal(r.code, 4);
      assert.equal(r.jsonError().code, "approval_outdated");
      assert.match(r.jsonError().message, /signature is out of date/);
      assert.match(r.jsonError().message, /in their own browser, at: http:\/\/127\.0\.0\.1:\d+\/connect\/muse\?intake=/);
      assert.match(r.jsonError().message, new RegExp(`tourclaim intake sign ${id} --wait$`));
    });

    it("falls back to the message when the header is missing", async () => {
      const old = new MockServer({ noConflictHeader: true });
      await old.start();
      try {
        const oldKey = old.issueKey({ traveler, channel: "cli" });
        const run = (argv: string[]) => runCli(argv, { home, apiUrl: old.url, env: { TOURCLAIM_API_KEY: oldKey } });
        await run(["intake", "start", "--json", "--idempotency-key", "fallback-0001", "--set", "booking_ref=A"]);
        let r = await run(["intake", "start", "--json", "--idempotency-key", "fallback-0001", "--set", "booking_ref=B"]);
        assert.equal(r.jsonError().code, "idempotency_key_reused");
        assert.match(r.jsonError().message, /new --idempotency-key/);
        const id = (await run(["intake", "start", "--json", ...COMPLETE.flatMap((p) => ["--set", p])])).json().id;
        r = await run(["intake", "set", id, "booking_ref=Z", "--revision", "5", "--json"]);
        assert.equal(r.jsonError().code, "stale_revision");
        old.sign(id);
        assert.equal((await run(["intake", "submit", id])).code, 0);
        r = await run(["intake", "delete", id, "--yes", "--json"]);
        assert.equal(r.code, 4);
        assert.equal(r.jsonError().code, "intake_submitted");
        assert.ok(!old.requests.some((q) => q.method === "PATCH" && q.headers["x-tourclaim-error"]));
      } finally {
        await old.stop();
      }
    });
  });

  describe("delete", () => {
    it("needs confirmation, then deletes; a submitted draft exits 4", async () => {
      const id = await start([]);
      let r = await cli(["intake", "delete", id]);
      assert.equal(r.code, 2);
      assert.ok(mock.intakes.has(id));
      r = await cli(["intake", "delete", id, "--yes", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.json(), { id, deleted: true });
      assert.ok(!mock.intakes.has(id));

      const submitted = await start();
      mock.sign(submitted);
      await cli(["intake", "submit", submitted]);
      r = await cli(["intake", "delete", submitted, "-y"]);
      assert.equal(r.code, 4);
      assert.match(r.stderr, /muse\/data-deletion/);
    });
  });

  describe("cards and schema", () => {
    it("searches cards and refuses card numbers", async () => {
      const r = await cli(["cards", "search", "sapphire", "--json"]);
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.json().map((c: { id: number }) => c.id), [412, 413]);
      assert.equal(mock.requestsTo("GET", "/api/connectors/v1/cards").at(-1)?.query.get("q"), "sapphire");
      const human = await cli(["cards", "search", "venture", "x"]);
      assert.match(human.stdout, /501\s+Capital One\s+Venture X/);
      assert.match(human.stdout, /does not mean it covers the loss/);
      const number = await cli(["cards", "search", "4111", "1111", "1111", "1111"]);
      assert.equal(number.code, 2);
    });

    it("prints the live OpenAPI schema without a key", async () => {
      const empty = await tempHome();
      const r = await runCli(["schema", "--json"], { home: empty.home, apiUrl: mock.url });
      assert.equal(r.code, 0, r.stderr);
      assert.equal(r.json().openapi, "3.1.0");
      assert.equal(r.json().info.title, "TourClaim by Copernican");
      await empty.cleanup();
    });
  });
});
