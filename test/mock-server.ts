/**
 * An in-process stand-in for the TourClaim connector API, implementing the
 * contract the CLI is written against: discovery, OpenAPI, device
 * authorization, key info and revocation, cards, intakes with revisions and
 * conflicts, evidence, signing (simulated), submission, claims, and the
 * API's error shapes (422 lists and strings, 401 with WWW-Authenticate, 403,
 * 404, 409, 413, 429 with Retry-After, 503).
 *
 * The traveler's browser is simulated by visiting /connect/cli?code=... (approves
 * a sign-in) and /connect/muse?intake=... (signs the current revision).
 */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer, type IncomingHttpHeaders, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const OPENAPI = JSON.parse(readFileSync(join(ROOT, "openapi", "connectors-v1.json"), "utf8")) as unknown;
const ALL_SCOPES = ["intakes:write", "evidence:write", "claims:submit", "claims:read"];
const MAX_BODY = 8 * 1024 * 1024;

export interface KeyRecord {
  token: string;
  id: string;
  /** "cli" for keys minted by tourclaim login; "key" for keys made at /connect/muse or by other apps. */
  channel: "cli" | "key";
  traveler: string;
  scopes: string[];
  expiresAt: Date;
  revoked: boolean;
}

export interface DeviceRecord {
  deviceCode: string;
  userCode: string;
  client: string;
  scopes: string[];
  createdAt: number;
  expiresIn: number;
  interval: number;
  status: "pending" | "approved" | "denied" | "consumed";
  traveler: string;
  description: string | null;
  lastPoll: number | null;
  polls: number;
}

export interface EvidenceRecord {
  id: string;
  kind: "email" | "attachment";
  sourceHash: string;
  data: string;
}

export interface IntakeRecord {
  id: string;
  keyId: string;
  traveler: string;
  idempotencyKey: string;
  requestHash: string;
  revision: number;
  fields: Record<string, unknown>;
  evidence: EvidenceRecord[];
  approvedRevision: number | null;
  claimId: string | null;
  reads: number;
  /** Set by tests: the authorization text changed after the traveler signed. */
  approvalOutdated?: boolean;
  /** Increases on every change; drafts list most recently changed first. */
  changed: number;
}

export interface ClaimRecord {
  id: string;
  intakeId: string;
  traveler: string;
  bookingKey: string;
  status: string;
  nextAction: string;
  updatedAt: string;
  seq: number;
}

export interface RecordedRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: IncomingHttpHeaders;
  body: unknown;
}

export interface MockOptions {
  enabled?: boolean;
  mode?: "review" | "live";
  deviceInterval?: number;
  deviceExpiresIn?: number;
  /** Answer slow_down when polled faster than the interval (real time). */
  enforceDeviceInterval?: boolean;
  /** Approve a pending sign-in after this many token polls (for npm run mock). */
  autoApproveDeviceAfterPolls?: number;
  /** Sign a draft awaiting approval after it has been read this many times. */
  autoSignAfterReads?: number;
  /** Behave like a server from before X-TourClaim-Error: 409s carry only a message. */
  noConflictHeader?: boolean;
  /** Behave like a server from before absolute discovery URLs. */
  relativeDiscovery?: boolean;
  traveler?: string;
}

interface Issue {
  loc: Array<string | number>;
  type: string;
  msg: string;
}

const REQUIRED_FIELDS: Record<string, string> = {
  merchant_name: "Who did you book with?",
  booking_ref: "What is the booking confirmation number?",
  trip_date: "What date was the trip or activity scheduled for?",
  booking_amount: "How much did you pay for this booking?",
  refunded_amount: "How much has already been refunded, including zero if nothing?",
  currency: "Was the booking paid in US dollars? This pilot supports USD bookings.",
  card_product_id: "Which credit card paid for the booking? Use the card catalog; never ask for a full card number.",
  reason_category: "Why were you unable to take the trip?",
  narrative: "Briefly describe what happened and when.",
  other_insurance: "Do you have separate travel insurance for this trip: yes, no, or unsure?",
};
const MEDICAL_QUESTIONS: Record<string, string> = {
  "medical.symptom_onset_date": "When did the symptoms or injury begin?",
  "medical.provider_seen": "Have you seen a healthcare provider about this illness or injury?",
  "medical.prior_condition": "Have you had this condition before?",
  "medical.stability_60day": "Were you treated for this condition in the 60 days before you booked the trip?",
};
const REASONS = ["ILLNESS", "INJURY", "DEATH_IN_FAMILY", "MILITARY_DEPLOYMENT", "WEATHER", "AIRLINE_CANCELLATION", "OTHER"];
const DECIMAL = /^(?!^[-+.]*$)[+-]?0*\d*\.?\d{0,2}0*$/;
const MAGIC: Record<string, number[]> = {
  "application/pdf": [0x25, 0x50, 0x44, 0x46, 0x2d],
  "image/jpeg": [0xff, 0xd8, 0xff],
  "image/png": [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a],
};

function zoned(date: Date): string {
  return date.toISOString().slice(0, 19) + "Z";
}

function sha256(text: string | Buffer): string {
  return createHash("sha256").update(text).digest("hex");
}

function newKeyToken(): string {
  return "tc_muse_" + randomBytes(36).toString("base64url");
}

function userCode(): string {
  const letters = "BCDFGHJKLMNPQRSTVWXZ";
  const bytes = randomBytes(8);
  const chars = [...bytes].map((b) => letters[b % letters.length]);
  return `${chars.slice(0, 4).join("")}-${chars.slice(4).join("")}`;
}

class HttpError {
  constructor(
    readonly status: number,
    readonly body: unknown,
    readonly headers: Record<string, string> = {},
  ) {}
}

const fail = (status: number, detail: unknown, headers: Record<string, string> = {}) => new HttpError(status, { detail }, headers);
let conflictHeaders = true;
const conflict = (code: string, detail: string) => fail(409, detail, conflictHeaders ? { "X-TourClaim-Error": code } : {});
const invalid = (issues: Issue[]) => new HttpError(422, { detail: issues });
const issue = (loc: Array<string | number>, type: string): Issue => ({ loc, type, msg: "Invalid field value" });

export class MockServer {
  url = "";
  readonly requests: RecordedRequest[] = [];
  readonly keys = new Map<string, KeyRecord>();
  readonly devices = new Map<string, DeviceRecord>();
  readonly intakes = new Map<string, IntakeRecord>();
  readonly claims = new Map<string, ClaimRecord>();
  readonly cards = [
    { id: 412, name: "Sapphire Preferred", issuer: "Chase" },
    { id: 413, name: "Sapphire Reserve", issuer: "Chase" },
    { id: 501, name: "Venture X", issuer: "Capital One" },
    { id: 900, name: "Synthetic Travel Card", issuer: "Example Bank" },
  ];
  enabled: boolean;
  mode: "review" | "live";
  options: MockOptions;
  /** The next N connector requests answer 429 with this Retry-After. */
  rateLimit = { remaining: 0, retryAfter: "1" as string | null };
  /** Forced answers for upcoming token polls, such as ["pending", "slow_down"]. */
  deviceScript: string[] = [];
  /** Called for every request before it is handled. */
  onRequest: ((request: RecordedRequest) => void) | null = null;
  /** Responses returned, in order, to the next connector requests instead of handling them. */
  inject: Array<{ status: number; body: unknown; headers?: Record<string, string> }> = [];
  private server: Server | null = null;
  private claimSeq = 0;
  private changeSeq = 0;

  constructor(options: MockOptions = {}) {
    this.options = options;
    this.enabled = options.enabled ?? true;
    this.mode = options.mode ?? "review";
  }

  get traveler(): string {
    return this.options.traveler ?? "pat@example.com";
  }

  async start(port = 0): Promise<string> {
    this.server = createServer((req, res) => {
      this.handle(req, res).catch((error: unknown) => {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ detail: `mock error: ${(error as Error).message}` }));
      });
    });
    await new Promise<void>((resolve) => this.server?.listen(port, "127.0.0.1", resolve));
    const address = this.server.address();
    if (!address || typeof address === "string") throw new Error("no address");
    this.url = `http://127.0.0.1:${address.port}`;
    return this.url;
  }

  async stop(): Promise<void> {
    if (!this.server) return;
    this.server.closeAllConnections?.();
    await new Promise<void>((resolve) => this.server?.close(() => resolve()));
    this.server = null;
  }

  // ---- test controls ----

  issueKey(options: { traveler?: string; scopes?: string[]; expiresInMs?: number; channel?: "cli" | "key" } = {}): string {
    const token = newKeyToken();
    this.keys.set(token, {
      token,
      id: randomUUID(),
      channel: options.channel ?? "key",
      traveler: options.traveler ?? this.traveler,
      scopes: options.scopes ?? ALL_SCOPES,
      expiresAt: new Date(Date.now() + (options.expiresInMs ?? 30 * 24 * 3_600_000)),
      revoked: false,
    });
    return token;
  }

  keyRecord(token: string): KeyRecord | undefined {
    return this.keys.get(token);
  }

  device(userCodeValue: string): DeviceRecord | undefined {
    return [...this.devices.values()].find((d) => d.userCode === userCodeValue);
  }

  approveDevice(userCodeValue: string, traveler = this.traveler): void {
    const d = this.device(userCodeValue);
    if (!d) throw new Error(`no device code ${userCodeValue}`);
    d.status = "approved";
    d.traveler = traveler;
  }

  denyDevice(userCodeValue: string, description = "The traveler declined this connection."): void {
    const d = this.device(userCodeValue);
    if (!d) throw new Error(`no device code ${userCodeValue}`);
    d.status = "denied";
    d.description = description;
  }

  /** The traveler signs the draft's current revision. */
  sign(intakeId: string): void {
    const rec = this.intakes.get(intakeId);
    if (!rec) throw new Error(`no intake ${intakeId}`);
    if (this.missing(rec.fields).length) throw new Error("cannot sign an incomplete draft");
    rec.approvedRevision = rec.revision;
  }

  requestsTo(method: string, path: string | RegExp): RecordedRequest[] {
    return this.requests.filter((r) => r.method === method && (typeof path === "string" ? r.path === path : path.test(r.path)));
  }

  // ---- request handling ----

  private async readBody(req: IncomingMessage): Promise<Buffer | null> {
    const chunks: Buffer[] = [];
    let size = 0;
    let tooLarge = false;
    for await (const chunk of req) {
      size += (chunk as Buffer).length;
      if (size > MAX_BODY) tooLarge = true;
      else chunks.push(chunk as Buffer);
    }
    return tooLarge ? null : Buffer.concat(chunks);
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://mock");
    const raw = await this.readBody(req);
    let body: unknown = undefined;
    if (raw && raw.length) {
      try {
        body = JSON.parse(raw.toString("utf8"));
      } catch {
        body = Symbol.for("invalid-json");
      }
    }
    const record: RecordedRequest = { method: req.method ?? "GET", path: url.pathname, query: url.searchParams, headers: req.headers, body };
    this.requests.push(record);
    this.onRequest?.(record);

    const isV1 = url.pathname.startsWith("/api/connectors/v1/") || url.pathname === "/api/connectors/v1";
    const headers: Record<string, string> = { "Cache-Control": "no-store" };
    if (isV1) headers["X-TourClaim-Mode"] = this.mode;
    conflictHeaders = !this.options.noConflictHeader;
    try {
      if (raw === null) throw fail(413, "Request exceeds 8 MiB");
      if (body === Symbol.for("invalid-json")) throw invalid([issue(["body"], "json_invalid")]);
      const result = this.route(record, url);
      if (result === undefined) {
        res.writeHead(204, headers);
        res.end();
      } else if (typeof result === "string") {
        res.writeHead(200, { ...headers, "Content-Type": "text/html; charset=utf-8" });
        res.end(result);
      } else {
        res.writeHead(200, { ...headers, "Content-Type": "application/json" });
        res.end(JSON.stringify(result));
      }
    } catch (error) {
      if (!(error instanceof HttpError)) throw error;
      res.writeHead(error.status, { ...headers, ...error.headers, "Content-Type": "application/json" });
      res.end(JSON.stringify(error.body));
    }
  }

  private route(r: RecordedRequest, url: URL): unknown {
    const { method, path } = r;
    if (path === "/api/connectors/v1" && method === "GET") {
      const base = this.options.relativeDiscovery ? "" : this.url;
      return {
        name: "TourClaim by Copernican",
        version: "1.0.0",
        enabled: this.enabled,
        mode: this.mode,
        openapi_url: `${base}/api/connectors/v1/openapi.json`,
        connection_url: `${base}/connect/muse`,
        ...(this.options.relativeDiscovery ? {} : { cli_login_url: `${base}/connect/cli` }),
        documentation_url: `${base}/muse/developers`,
      };
    }
    if (path === "/api/connectors/v1/openapi.json" && method === "GET") return OPENAPI;
    if (path === "/connect/cli" && method === "GET") return this.browserApprove(url.searchParams.get("code"));
    if (path === "/connect/muse" && method === "GET") return this.browserSign(url.searchParams.get("intake"));

    if (!path.startsWith("/api/connectors/")) throw fail(404, "Not Found");
    if (!this.enabled) throw fail(503, "Muse connector pilot is not enabled");
    if (this.rateLimit.remaining > 0) {
      this.rateLimit.remaining--;
      throw new HttpError(
        429,
        { error: "Rate limit exceeded: 60 per 1 minute" },
        this.rateLimit.retryAfter === null ? {} : { "Retry-After": this.rateLimit.retryAfter },
      );
    }

    const injected = this.inject.shift();
    if (injected) throw new HttpError(injected.status, injected.body, injected.headers);

    if (path === "/api/connectors/device/code" && method === "POST") return this.deviceCode(r.body);
    if (path === "/api/connectors/device/token" && method === "POST") return this.deviceToken(r.body);

    const key = this.authenticate(r);
    const v1 = path.slice("/api/connectors/v1".length);
    if (v1 === "/key" && method === "GET") {
      return { id: key.id, expires_at: zoned(key.expiresAt), scopes: key.scopes, account_email: key.traveler };
    }
    if (v1 === "/key" && method === "DELETE") {
      key.revoked = true;
      return undefined;
    }
    if (v1 === "/cards" && method === "GET") {
      this.scope(key, "intakes:write");
      const q = url.searchParams.get("q") ?? "";
      if (q.length > 100) throw invalid([issue(["query", "q"], "string_too_long")]);
      const needle = q.toLowerCase();
      return this.cards
        .filter((c) => `${c.issuer} ${c.name}`.toLowerCase().includes(needle))
        .slice(0, 30)
        .map((c) => ({ ...c, coverage_status: "requires_review" }));
    }
    if (v1 === "/intakes" && method === "GET") {
      this.scope(key, "intakes:write");
      const offsetText = url.searchParams.get("offset") ?? "0";
      if (!/^\d+$/.test(offsetText)) throw invalid([issue(["query", "offset"], "int_parsing")]);
      const offset = Number(offsetText);
      return [...this.intakes.values()]
        .filter((i) => i.traveler === key.traveler && !i.claimId && this.reaches(key, i))
        .sort((a, b) => b.changed - a.changed)
        .slice(offset, offset + 30)
        .map((i) => this.intakeResponse(i));
    }
    if (v1 === "/intakes" && method === "POST") {
      this.scope(key, "intakes:write");
      return this.startIntake(key, r);
    }
    let m = /^\/intakes\/([^/]+)(\/[a-z-]+)?$/.exec(v1);
    if (m) {
      const id = decodeURIComponent(m[1] ?? "");
      const action = m[2] ?? "";
      if (action === "" && method === "GET") {
        this.scope(key, "intakes:write");
        const rec = this.owned(key, id);
        this.maybeAutoSign(rec);
        return this.intakeResponse(rec);
      }
      if (action === "" && method === "PATCH") {
        this.scope(key, "intakes:write");
        return this.update(this.owned(key, id), r.body);
      }
      if (action === "" && method === "DELETE") {
        this.scope(key, "intakes:write");
        const rec = this.owned(key, id);
        if (rec.claimId) throw conflict("intake_submitted", "A submitted claim cannot be deleted here. See the data deletion instructions to request deletion.");
        this.intakes.delete(rec.id);
        return undefined;
      }
      if (action === "/email-evidence" && method === "POST") {
        this.scope(key, "evidence:write");
        return this.addEmail(this.owned(key, id), r.body);
      }
      if (action === "/attachments" && method === "POST") {
        this.scope(key, "evidence:write");
        return this.addAttachment(this.owned(key, id), r.body);
      }
      if (action === "/submit" && method === "POST") {
        this.scope(key, "claims:submit");
        return this.submit(this.owned(key, id), r.body);
      }
    }
    if (v1 === "/claims" && method === "GET") {
      this.scope(key, "claims:read");
      const offsetText = url.searchParams.get("offset") ?? "0";
      if (!/^\d+$/.test(offsetText)) throw invalid([issue(["query", "offset"], "int_parsing")]);
      const offset = Number(offsetText);
      return [...this.claims.values()]
        .filter((c) => c.traveler === key.traveler)
        .sort((a, b) => b.seq - a.seq)
        .slice(offset, offset + 30)
        .map((c) => this.claimResponse(c));
    }
    m = /^\/claims\/([^/]+)$/.exec(v1);
    if (m && method === "GET") {
      this.scope(key, "claims:read");
      const claim = this.claims.get(decodeURIComponent(m[1] ?? ""));
      if (!claim || claim.traveler !== key.traveler) throw fail(404, "Claim not found");
      return this.claimResponse(claim);
    }
    throw fail(404, "Not Found");
  }

  // ---- device authorization ----

  private deviceCode(body: unknown): unknown {
    const b = (body ?? {}) as Record<string, unknown>;
    if (typeof b.client !== "string" || !b.client) throw invalid([issue(["body", "client"], "missing")]);
    let scopes = ALL_SCOPES;
    if (b.scopes !== undefined) {
      if (!Array.isArray(b.scopes) || b.scopes.some((s) => typeof s !== "string" || !ALL_SCOPES.includes(s))) {
        throw invalid([issue(["body", "scopes"], "enum")]);
      }
      scopes = b.scopes as string[];
    }
    const record: DeviceRecord = {
      deviceCode: randomBytes(32).toString("base64url"),
      userCode: userCode(),
      client: b.client,
      scopes,
      createdAt: Date.now(),
      expiresIn: this.options.deviceExpiresIn ?? 600,
      interval: this.options.deviceInterval ?? 5,
      status: "pending",
      traveler: this.traveler,
      description: null,
      lastPoll: null,
      polls: 0,
    };
    this.devices.set(record.deviceCode, record);
    const verification = `${this.url}/connect/cli`;
    return {
      device_code: record.deviceCode,
      user_code: record.userCode,
      verification_uri: verification,
      verification_uri_complete: `${verification}?code=${record.userCode}`,
      expires_in: record.expiresIn,
      interval: record.interval,
    };
  }

  private deviceToken(body: unknown): unknown {
    const b = (body ?? {}) as Record<string, unknown>;
    if (typeof b.device_code !== "string") throw new HttpError(400, { error: "invalid_request" });
    const d = this.devices.get(b.device_code);
    const scripted = this.deviceScript.shift();
    if (scripted === "pending") throw new HttpError(400, { error: "authorization_pending" });
    if (scripted === "slow_down") throw new HttpError(400, { error: "slow_down" });
    if (scripted === "denied") throw new HttpError(400, { error: "access_denied", error_description: "The traveler declined this connection." });
    if (scripted === "expired") throw new HttpError(400, { error: "expired_token" });
    if (scripted === "approve" && d) d.status = "approved";
    if (!d || d.status === "consumed" || Date.now() > d.createdAt + d.expiresIn * 1000) {
      throw new HttpError(400, { error: "expired_token" });
    }
    const now = Date.now();
    if (this.options.enforceDeviceInterval && d.lastPoll !== null && now - d.lastPoll < d.interval * 1000 - 50) {
      d.lastPoll = now;
      d.interval += 5;
      throw new HttpError(400, { error: "slow_down" });
    }
    d.lastPoll = now;
    d.polls++;
    if (d.status === "pending" && this.options.autoApproveDeviceAfterPolls && d.polls >= this.options.autoApproveDeviceAfterPolls) {
      d.status = "approved";
    }
    if (d.status === "denied") throw new HttpError(400, { error: "access_denied", error_description: d.description ?? "Declined." });
    if (d.status === "pending") throw new HttpError(400, { error: "authorization_pending" });
    d.status = "consumed";
    const token = this.issueKey({ traveler: d.traveler, scopes: d.scopes, channel: "cli" });
    const key = this.keys.get(token) as KeyRecord;
    return { api_key: token, expires_at: zoned(key.expiresAt), scopes: key.scopes, grant_id: key.id };
  }

  private browserApprove(code: string | null): string {
    const d = code ? this.device(code) : undefined;
    if (!d || d.status !== "pending") return "<!doctype html><title>TourClaim mock</title><p>No pending sign-in with that code.</p>";
    d.status = "approved";
    return `<!doctype html><title>TourClaim mock</title><p>Mock: approved sign-in ${d.userCode} for ${d.traveler}. Return to the terminal.</p>`;
  }

  private browserSign(intakeId: string | null): string {
    const rec = intakeId ? this.intakes.get(intakeId) : undefined;
    if (!rec || rec.claimId || this.missing(rec.fields).length) {
      return "<!doctype html><title>TourClaim mock</title><p>Nothing to sign for that draft.</p>";
    }
    rec.approvedRevision = rec.revision;
    return `<!doctype html><title>TourClaim mock</title><p>Mock: the traveler signed revision ${rec.revision} of draft ${rec.id}.</p>`;
  }

  // ---- auth ----

  private authenticate(r: RecordedRequest): KeyRecord {
    const header = r.headers.authorization ?? "";
    const match = /^Bearer (.+)$/.exec(header);
    const unauthorized = (detail: string) => fail(401, detail, { "WWW-Authenticate": "Bearer" });
    if (!match || !match[1]?.startsWith("tc_muse_")) throw unauthorized("Muse connection required");
    const key = this.keys.get(match[1]);
    if (!key || key.revoked || key.expiresAt.getTime() < Date.now()) throw unauthorized("Muse connection expired or disconnected");
    return key;
  }

  private scope(key: KeyRecord, name: string): void {
    if (!key.scopes.includes(name)) throw fail(403, "Connection does not permit this action");
  }

  /**
   * A key reaches the drafts it started. A CLI key also reaches drafts any of
   * the same traveler's CLI keys started, even revoked or expired ones.
   */
  private reaches(key: KeyRecord, rec: IntakeRecord): boolean {
    if (rec.keyId === key.id) return true;
    const starter = [...this.keys.values()].find((k) => k.id === rec.keyId);
    return key.channel === "cli" && starter?.channel === "cli" && starter.traveler === key.traveler;
  }

  private owned(key: KeyRecord, id: string): IntakeRecord {
    const rec = this.intakes.get(id);
    if (!rec || rec.traveler !== key.traveler || !this.reaches(key, rec)) throw fail(404, "Intake not found for this connection");
    return rec;
  }

  // ---- intakes ----

  private validateFields(fields: unknown, loc: Array<string | number>): Issue[] {
    if (fields === undefined) return [];
    if (!fields || typeof fields !== "object" || Array.isArray(fields)) return [issue(loc, "model_type")];
    const issues: Issue[] = [];
    const str = (k: string, v: unknown, min: number, max: number) => {
      if (typeof v !== "string") issues.push(issue([...loc, k], "string_type"));
      else if (v.length < min) issues.push(issue([...loc, k], "string_too_short"));
      else if (v.length > max) issues.push(issue([...loc, k], "string_too_long"));
    };
    for (const [k, v] of Object.entries(fields as Record<string, unknown>)) {
      if (v === null) continue;
      switch (k) {
        case "merchant_name":
          str(k, v, 1, 255);
          break;
        case "booking_ref":
          str(k, v, 1, 100);
          break;
        case "trip_date":
          if (typeof v !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(v)) issues.push(issue([...loc, k], "date_from_datetime_parsing"));
          break;
        case "booking_amount":
        case "refunded_amount":
          if (!(typeof v === "number" && v >= 0) && !(typeof v === "string" && DECIMAL.test(v))) issues.push(issue([...loc, k], "decimal_parsing"));
          break;
        case "currency":
          if (v !== "USD") issues.push(issue([...loc, k], "literal_error"));
          break;
        case "card_product_id":
          if (typeof v !== "number" || !Number.isInteger(v) || v <= 0) issues.push(issue([...loc, k], "int_type"));
          break;
        case "reason_category":
          if (typeof v !== "string" || !REASONS.includes(v)) issues.push(issue([...loc, k], "enum"));
          break;
        case "narrative":
          str(k, v, 10, 10000);
          break;
        case "cancellation_policy":
          str(k, v, 0, 10000);
          break;
        case "other_insurance":
          if (v !== "YES" && v !== "NO" && v !== "UNSURE") issues.push(issue([...loc, k], "enum"));
          break;
        case "other_insurance_details":
          str(k, v, 0, 500);
          break;
        case "medical": {
          if (typeof v !== "object" || Array.isArray(v)) {
            issues.push(issue([...loc, k], "model_type"));
            break;
          }
          for (const [mk, mv] of Object.entries(v as Record<string, unknown>)) {
            if (mv === null) continue;
            if (mk === "symptom_onset_date") {
              if (typeof mv !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(mv)) issues.push(issue([...loc, k, mk], "date_from_datetime_parsing"));
            } else if (mk === "provider_seen" || mk === "prior_condition" || mk === "stability_60day") {
              if (typeof mv !== "boolean") issues.push(issue([...loc, k, mk], "bool_type"));
            } else if (mk === "provider_details" || mk === "prior_condition_details" || mk === "stability_60day_details") {
              if (typeof mv !== "string" || mv.length > 2000) issues.push(issue([...loc, k, mk], "string_type"));
            } else {
              issues.push(issue([...loc, k, mk], "extra_forbidden"));
            }
          }
          break;
        }
        default:
          issues.push(issue([...loc, k], "extra_forbidden"));
      }
    }
    return issues;
  }

  private normalize(fields: Record<string, unknown>): Record<string, unknown> {
    const out = { ...fields };
    for (const k of ["booking_amount", "refunded_amount"]) {
      if (typeof out[k] === "number") out[k] = (out[k] as number).toFixed(2);
    }
    return out;
  }

  private refundTooHigh(fields: Record<string, unknown>): boolean {
    const paid = Number(fields.booking_amount);
    const refunded = Number(fields.refunded_amount);
    return fields.booking_amount != null && fields.refunded_amount != null && refunded > paid;
  }

  private checkCard(fields: Record<string, unknown>): void {
    const id = fields.card_product_id;
    if (id != null && !this.cards.some((c) => c.id === id)) throw fail(422, "Unknown card_product_id; search the card catalog");
  }

  private missing(fields: Record<string, unknown>): string[] {
    const missing = Object.keys(REQUIRED_FIELDS).filter((k) => fields[k] === null || fields[k] === undefined);
    if (fields.other_insurance === "YES" && !fields.other_insurance_details) missing.push("other_insurance_details");
    if (fields.reason_category === "ILLNESS" || fields.reason_category === "INJURY") {
      const medical = (fields.medical ?? {}) as Record<string, unknown>;
      for (const name of Object.keys(MEDICAL_QUESTIONS)) {
        const sub = name.split(".")[1] ?? "";
        if (medical[sub] === null || medical[sub] === undefined) missing.push(name);
      }
    }
    return missing;
  }

  intakeResponse(rec: IntakeRecord): Record<string, unknown> {
    const missing = this.missing(rec.fields);
    let state: string;
    if (rec.claimId) state = "submitted";
    else if (missing.length) state = "collecting";
    else if (rec.approvedRevision === rec.revision && !rec.approvalOutdated) state = "ready_to_submit";
    else state = "needs_approval";
    const counts: Record<string, number> = {};
    const evidence = rec.evidence.map((e) => {
      counts[e.kind] = (counts[e.kind] ?? 0) + 1;
      return { id: e.id, kind: e.kind, label: `${e.kind === "email" ? "Email" : "File"} evidence ${counts[e.kind]}` };
    });
    const medical = rec.fields.reason_category === "ILLNESS" || rec.fields.reason_category === "INJURY";
    const link = `${this.url}/connect/muse?intake=${rec.id}`;
    const fields: Record<string, unknown> = {};
    for (const k of [...Object.keys(REQUIRED_FIELDS), "cancellation_policy", "other_insurance_details", "medical"]) {
      fields[k] = rec.fields[k] ?? null;
    }
    return {
      mode: this.mode,
      id: rec.id,
      revision: rec.revision,
      fields,
      missing_fields: missing,
      next_questions: missing.map((n) => ({ ...REQUIRED_FIELDS, ...MEDICAL_QUESTIONS })[n] ?? "Which insurer and what does the policy cover?").slice(0, 3),
      evidence,
      state,
      review_url: state === "needs_approval" ? link : null,
      evidence_upload_url: state === "submitted" ? null : link,
      claim_id: rec.claimId,
      coverage_status: "not_determined",
      medical_note_pathway: medical
        ? "Copernican can arrange medical documentation review; a provider determines whether a note can be issued."
        : "Medical documentation requirements will be reviewed by Copernican.",
    };
  }

  private startIntake(key: KeyRecord, r: RecordedRequest): unknown {
    const idem = r.headers["idempotency-key"];
    const issues: Issue[] = [];
    if (typeof idem !== "string") issues.push(issue(["header", "idempotency-key"], "missing"));
    else if (!/^[A-Za-z0-9_-]{8,100}$/.test(idem)) issues.push(issue(["header", "idempotency-key"], "string_pattern_mismatch"));
    const body = (r.body ?? {}) as Record<string, unknown>;
    if (typeof body !== "object" || Array.isArray(body)) issues.push(issue(["body"], "model_type"));
    for (const k of Object.keys(body)) if (k !== "fields") issues.push(issue(["body", k], "extra_forbidden"));
    issues.push(...this.validateFields(body.fields, ["body", "fields"]));
    if (issues.length) throw invalid(issues);
    const fields = this.normalize((body.fields ?? {}) as Record<string, unknown>);
    if (this.refundTooHigh(fields)) throw invalid([issue(["body", "fields"], "value_error")]);
    const requestHash = sha256(JSON.stringify(fields, Object.keys(fields).sort()));
    const existing = [...this.intakes.values()].find((i) => i.traveler === key.traveler && i.idempotencyKey === idem);
    if (existing) {
      if (existing.requestHash !== requestHash) throw conflict("idempotency_key_reused", "Idempotency key was already used with different fields");
      if (existing.keyId !== key.id) throw conflict("idempotency_key_other_connection", "This intake belongs to an earlier connection; use a new idempotency key");
      return this.intakeResponse(existing);
    }
    this.checkCard(fields);
    const rec: IntakeRecord = {
      id: randomUUID(),
      keyId: key.id,
      traveler: key.traveler,
      idempotencyKey: idem as string,
      requestHash,
      revision: 1,
      fields,
      evidence: [],
      approvedRevision: null,
      claimId: null,
      reads: 0,
      changed: ++this.changeSeq,
    };
    this.intakes.set(rec.id, rec);
    return this.intakeResponse(rec);
  }

  private checkRevisionBody(body: unknown, allowed: string[]): { revision: number; body: Record<string, unknown> } {
    const b = (body ?? {}) as Record<string, unknown>;
    const issues: Issue[] = [];
    if (!Number.isInteger(b.expected_revision) || (b.expected_revision as number) < 1) issues.push(issue(["body", "expected_revision"], "int_type"));
    for (const k of Object.keys(b)) if (!allowed.includes(k) && k !== "expected_revision") issues.push(issue(["body", k], "extra_forbidden"));
    if (issues.length) throw invalid(issues);
    return { revision: b.expected_revision as number, body: b };
  }

  private changeRevision(rec: IntakeRecord, expected: number): void {
    if (rec.claimId) throw conflict("intake_submitted", "Submitted intake is read-only");
    if (rec.revision !== expected) throw conflict("stale_revision", "Intake changed; retrieve the current revision and try again");
    rec.revision += 1;
    rec.approvedRevision = null;
    rec.changed = ++this.changeSeq;
  }

  private update(rec: IntakeRecord, body: unknown): unknown {
    const { revision, body: b } = this.checkRevisionBody(body, ["fields"]);
    if (b.fields === undefined) throw invalid([issue(["body", "fields"], "missing")]);
    const issues = this.validateFields(b.fields, ["body", "fields"]);
    if (issues.length) throw invalid(issues);
    const patch = this.normalize(b.fields as Record<string, unknown>);
    if (patch.medical && typeof patch.medical === "object") {
      patch.medical = { ...((rec.fields.medical as Record<string, unknown> | null) ?? {}), ...(patch.medical as Record<string, unknown>) };
    }
    const merged = { ...rec.fields, ...patch };
    if (this.refundTooHigh(merged)) throw fail(422, "Updated fields are inconsistent; refund cannot exceed amount paid");
    this.checkCard(merged);
    this.changeRevision(rec, revision);
    rec.fields = merged;
    return this.intakeResponse(rec);
  }

  private addEvidence(rec: IntakeRecord, revision: number, kind: "email" | "attachment", sourceHash: string, data: string): unknown {
    const previous = rec.evidence.find((e) => e.sourceHash === sourceHash);
    if (previous) {
      if (previous.data !== data) throw conflict("evidence_conflict", "Evidence source was already imported with different contents");
      return this.intakeResponse(rec);
    }
    if (rec.evidence.length >= 30) throw fail(422, "An intake supports at most 30 evidence items");
    this.changeRevision(rec, revision);
    rec.evidence.push({ id: randomUUID(), kind, sourceHash, data });
    return this.intakeResponse(rec);
  }

  private addEmail(rec: IntakeRecord, body: unknown): unknown {
    const allowed = ["provider", "message_id", "subject", "sender", "sent_at", "text", "user_authorized_sharing"];
    const { revision, body: b } = this.checkRevisionBody(body, allowed);
    const issues: Issue[] = [];
    if (!["gmail", "outlook", "user"].includes(b.provider as string)) issues.push(issue(["body", "provider"], "enum"));
    if (typeof b.message_id !== "string" || !b.message_id || b.message_id.length > 255) issues.push(issue(["body", "message_id"], "string_type"));
    if (typeof b.subject !== "string" || b.subject.length > 500) issues.push(issue(["body", "subject"], "string_type"));
    if (typeof b.sender !== "string" || b.sender.length > 320) issues.push(issue(["body", "sender"], "string_type"));
    if (b.sent_at !== undefined && b.sent_at !== null && (typeof b.sent_at !== "string" || Number.isNaN(Date.parse(b.sent_at)))) {
      issues.push(issue(["body", "sent_at"], "datetime_parsing"));
    }
    if (typeof b.text !== "string" || !b.text || [...b.text].length > 30000) issues.push(issue(["body", "text"], "string_type"));
    if (b.user_authorized_sharing !== true) issues.push(issue(["body", "user_authorized_sharing"], "literal_error"));
    if (issues.length) throw invalid(issues);
    const data = JSON.stringify({ provider: b.provider, message_id: b.message_id, subject: b.subject, sender: b.sender, sent_at: b.sent_at ?? null, text: b.text });
    return this.addEvidence(rec, revision, "email", sha256(`${b.provider}:${b.message_id}`), data);
  }

  private addAttachment(rec: IntakeRecord, body: unknown): unknown {
    const allowed = ["filename", "content_type", "doc_type", "content_base64", "user_authorized_sharing"];
    const { revision, body: b } = this.checkRevisionBody(body, allowed);
    const issues: Issue[] = [];
    if (typeof b.filename !== "string" || !/^[^/\\\x00-\x1f]+$/.test(b.filename) || b.filename.length > 200) issues.push(issue(["body", "filename"], "string_pattern_mismatch"));
    if (!Object.keys(MAGIC).includes(b.content_type as string)) issues.push(issue(["body", "content_type"], "enum"));
    if (!["receipt", "itinerary", "medical_note", "other"].includes(b.doc_type as string)) issues.push(issue(["body", "doc_type"], "enum"));
    if (typeof b.content_base64 !== "string" || b.content_base64.length < 4 || b.content_base64.length > 6990508) issues.push(issue(["body", "content_base64"], "string_type"));
    if (typeof b.user_authorized_sharing !== "boolean") issues.push(issue(["body", "user_authorized_sharing"], "bool_type"));
    if (issues.length) throw invalid(issues);
    if (b.user_authorized_sharing !== true) throw fail(422, "Ask the traveler before sharing evidence");
    const content = Buffer.from(b.content_base64 as string, "base64");
    if (content.toString("base64").replace(/=+$/, "") !== (b.content_base64 as string).replace(/=+$/, "")) throw fail(422, "Attachment must be valid base64");
    const magic = MAGIC[b.content_type as string] ?? [];
    if (content.length > 5 * 1024 * 1024 || !magic.every((byte, i) => content[i] === byte)) throw fail(422, "Attachment size or file type is invalid");
    const data = JSON.stringify({ filename: b.filename, content_type: b.content_type, doc_type: b.doc_type });
    return this.addEvidence(rec, revision, "attachment", sha256(content), data);
  }

  private maybeAutoSign(rec: IntakeRecord): void {
    const after = this.options.autoSignAfterReads;
    if (!after || rec.claimId || this.missing(rec.fields).length || rec.approvedRevision === rec.revision) return;
    rec.reads++;
    if (rec.reads >= after) {
      rec.approvedRevision = rec.revision;
      rec.reads = 0;
    }
  }

  private submit(rec: IntakeRecord, body: unknown): unknown {
    const { revision } = this.checkRevisionBody(body, []);
    if (rec.claimId) return this.claimResponse(this.claims.get(rec.claimId) as ClaimRecord);
    // Same order as the server: a revision mismatch is reported as approval_required.
    if (rec.revision !== revision) throw conflict("approval_required", "The traveler must approve this exact intake revision");
    // Simulates the authorization text changing after the traveler signed.
    if (rec.approvalOutdated) throw conflict("approval_outdated", "Authorization changed; ask the traveler to review again");
    if (rec.approvedRevision !== rec.revision) throw conflict("approval_required", "The traveler must approve this exact intake revision");
    if (this.missing(rec.fields).length) throw conflict("intake_incomplete", "Intake is incomplete");
    const bookingKey = `${rec.traveler}|${String(rec.fields.merchant_name).toLowerCase()}|${String(rec.fields.booking_ref).toLowerCase()}`;
    if ([...this.claims.values()].some((c) => c.bookingKey === bookingKey)) throw conflict("duplicate_booking", "A claim for this booking already exists");
    const claim: ClaimRecord = {
      id: randomUUID(),
      intakeId: rec.id,
      traveler: rec.traveler,
      bookingKey,
      status: "INTAKE_RECEIVED",
      nextAction:
        this.mode === "review"
          ? "Synthetic review claim received. Nothing will be filed."
          : "Copernican is reviewing your claim and evidence.",
      updatedAt: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      seq: ++this.claimSeq,
    };
    this.claims.set(claim.id, claim);
    rec.claimId = claim.id;
    return this.claimResponse(claim);
  }

  private claimResponse(c: ClaimRecord): unknown {
    return { mode: this.mode, id: c.id, intake_id: c.intakeId, status: c.status, next_action: c.nextAction, updated_at: c.updatedAt };
  }
}
