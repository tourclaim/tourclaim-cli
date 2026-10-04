import { ApiClient } from "./api.js";
import { userAgent } from "./config.js";
import { CredentialStore, credentialsPath, type StoredCredential } from "./credentials.js";
import type { Deps } from "./deps.js";
import { CliError, ExitCode } from "./errors.js";
import { formatTime, parseServerTime } from "./format.js";
import type { Output } from "./output.js";

const EXPIRY_WARNING_MS = 3 * 24 * 3_600_000;

export interface ActiveKey {
  key: string;
  source: "env" | "stored";
  stored: StoredCredential | null;
}

export class Context {
  readonly store: CredentialStore;
  readonly userAgent: string;
  private activeKeyCache: ActiveKey | null | undefined;
  private expiryWarned = false;

  constructor(
    readonly deps: Deps,
    readonly out: Output,
    readonly apiUrl: string,
  ) {
    this.store = new CredentialStore(credentialsPath(deps.env, deps.platform, deps.homedir), deps.platform, (msg) => out.warn(msg));
    this.userAgent = userAgent(deps.platform, deps.arch, deps.nodeVersion);
  }

  get json(): boolean {
    return this.out.json;
  }

  /** The key in effect: TOURCLAIM_API_KEY, else the credential stored for this API URL. */
  async activeKey(): Promise<ActiveKey | null> {
    if (this.activeKeyCache !== undefined) return this.activeKeyCache;
    const envKey = this.deps.env.TOURCLAIM_API_KEY?.trim();
    const stored = await this.store.get(this.apiUrl);
    if (stored) this.out.addSecret(stored.api_key);
    if (envKey) {
      this.out.addSecret(envKey);
      this.activeKeyCache = { key: envKey, source: "env", stored };
    } else if (stored) {
      this.activeKeyCache = { key: stored.api_key, source: "stored", stored };
    } else {
      this.activeKeyCache = null;
    }
    return this.activeKeyCache;
  }

  forgetActiveKey(): void {
    this.activeKeyCache = undefined;
  }

  /** A client for unauthenticated calls, or for a key given explicitly. */
  client(apiKey: string | null = null): ApiClient {
    if (apiKey) this.out.addSecret(apiKey);
    return new ApiClient({
      baseUrl: this.apiUrl,
      apiKey,
      userAgent: this.userAgent,
      fetch: this.deps.fetch,
      sleep: this.deps.sleep,
      notice: (msg) => this.out.info(msg),
      missingKey: () => this.notSignedIn(),
    });
  }

  /** A client that sends the active key. Fails with exit 3 when there is none. */
  async authed(): Promise<ApiClient> {
    const active = await this.activeKey();
    if (!active) throw this.notSignedIn();
    if (active.source === "stored") this.warnIfExpiring(active.stored);
    return this.client(active.key);
  }

  notSignedIn(): CliError {
    return new CliError(
      `Not signed in to ${this.apiUrl}. Run: tourclaim login`,
      ExitCode.AUTH,
      "not_signed_in",
    );
  }

  private warnIfExpiring(stored: StoredCredential | null): void {
    if (this.expiryWarned || !stored?.expires_at) return;
    const expires = parseServerTime(stored.expires_at);
    if (!expires) return;
    const left = expires.getTime() - this.deps.now();
    if (left > EXPIRY_WARNING_MS) return;
    this.expiryWarned = true;
    if (left <= 0) {
      this.out.warn(`Your TourClaim key expired on ${formatTime(stored.expires_at)}. Run: tourclaim login`);
    } else {
      const days = Math.floor(left / (24 * 3_600_000));
      const when = days >= 1 ? `in ${days} day${days === 1 ? "" : "s"}` : "within a day";
      this.out.warn(
        `Your TourClaim key expires ${when} (${formatTime(stored.expires_at)}). Keys are not renewed; to get a new one now, run: tourclaim login --force`,
      );
    }
  }
}
