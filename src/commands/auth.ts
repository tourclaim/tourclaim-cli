import { API_PREFIX, ApiError, sentences, type ApiClient } from "../api.js";
import { flag, strs, type Command } from "../command.js";
import { absoluteUrl } from "../config.js";
import type { Context } from "../context.js";
import type { StoredCredential } from "../credentials.js";
import { CliError, ExitCode, UsageError } from "../errors.js";
import { formatTime, modeNotice, relativeTime } from "../format.js";
import {
  SCOPES,
  type ConnectorInfo,
  type DeviceCodeResponse,
  type DeviceTokenError,
  type DeviceTokenSuccess,
  type KeyInfo,
} from "../types.js";

const KEY_PATH = `${API_PREFIX}/key`;
const DEVICE_CODE_PATH = "/api/connectors/device/code";
const DEVICE_TOKEN_PATH = "/api/connectors/device/token";
const KEY_FORMAT = /^tc_muse_[A-Za-z0-9_-]+$/;
const MAX_POLL_FAILURES = 3;

function manageKeysUrl(ctx: Context): string {
  return absoluteUrl(ctx.apiUrl, "/connect/muse");
}

function parseScopes(values: string[]): string[] {
  const scopes = values.flatMap((v) => v.split(",")).map((s) => s.trim()).filter(Boolean);
  for (const scope of scopes) {
    if (!(SCOPES as readonly string[]).includes(scope)) {
      throw new UsageError(`Unknown scope "${scope}". Scopes: ${SCOPES.join(", ")}.`);
    }
  }
  return [...new Set(scopes)];
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
}

/** GET /v1/key with a given key. Null when the key is rejected. */
async function keyInfo(ctx: Context, key: string): Promise<{ info: KeyInfo; mode: string | null } | null> {
  try {
    const res = await ctx.client(key).request<KeyInfo>(KEY_PATH);
    return { info: res.data, mode: res.headers.get("x-tourclaim-mode") };
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return null;
    throw error;
  }
}

interface SignedIn {
  info: KeyInfo | null;
  mode: string | null;
  credential: StoredCredential;
  replacedKeyId: string | null;
  already: boolean;
}

function report(ctx: Context, result: SignedIn): void {
  const expiresAt = result.info?.expires_at ?? result.credential.expires_at;
  const scopes = result.info?.scopes ?? result.credential.scopes;
  const email = result.info?.account_email ?? null;
  if (ctx.json) {
    ctx.out.data({
      event: "signed_in",
      already_signed_in: result.already,
      api_url: ctx.apiUrl,
      account_email: email,
      key: { id: result.info?.id ?? result.credential.grant_id, expires_at: expiresAt, scopes },
      grant_id: result.credential.grant_id,
      mode: result.mode,
      credentials_path: ctx.store.path,
      replaced_key_id: result.replacedKeyId,
    });
    return;
  }
  const expiry = `key expires ${formatTime(expiresAt)}, ${relativeTime(expiresAt, ctx.deps.now())}`;
  const who = email ? `Signed in as ${email}` : `Signed in to ${ctx.apiUrl}`;
  ctx.out.line(result.already ? `Already signed in as ${email ?? "this traveler"} (${expiry}).` : `${who} (${expiry}).`);
  if (scopes.length) ctx.out.line(`Permissions: ${scopes.join(", ")}`);
  if (!result.already) ctx.out.line(`Key saved to ${ctx.store.path} (readable only by you).`);
  if (result.replacedKeyId) ctx.out.line(`Revoked the previous key ${result.replacedKeyId}.`);
  const notice = modeNotice(result.mode);
  if (notice) ctx.out.line(notice.charAt(0).toUpperCase() + notice.slice(1) + ".");
  if (result.already) ctx.out.line("To replace this key, run: tourclaim login --force");
}

async function pollForToken(ctx: Context, api: ApiClient, code: DeviceCodeResponse): Promise<DeviceTokenSuccess> {
  let interval = Math.max(1, Number(code.interval) || 5);
  const deadline = ctx.deps.now() + code.expires_in * 1000;
  let failures = 0;
  for (;;) {
    if (ctx.deps.now() + interval * 1000 > deadline) {
      throw new CliError("The sign-in code expired before it was approved. Run tourclaim login again.", ExitCode.AUTH, "expired_token");
    }
    await ctx.deps.sleep(interval * 1000);
    let res;
    try {
      res = await api.request<DeviceTokenSuccess | DeviceTokenError>(DEVICE_TOKEN_PATH, {
        method: "POST",
        body: { device_code: code.device_code },
        auth: false,
        allowStatus: [400, 429],
        retryRateLimit: false,
      });
      failures = 0;
    } catch (error) {
      const transient = error instanceof CliError && (error.code === "network_error" || error.code === "timeout");
      if (transient && ++failures < MAX_POLL_FAILURES) {
        interval = Math.min(interval * 2, 60);
        continue;
      }
      throw error;
    }
    if (res.status === 200) return res.data as DeviceTokenSuccess;
    if (res.status === 429) {
      interval += 5;
      continue;
    }
    const err = (res.data ?? {}) as DeviceTokenError;
    switch (err.error) {
      case "authorization_pending":
        continue;
      case "slow_down":
        interval += 5;
        continue;
      case "access_denied":
        throw new CliError(
          sentences("The sign-in was declined", err.error_description, "Nothing was saved."),
          ExitCode.AUTH,
          "access_denied",
        );
      case "expired_token":
        throw new CliError("The sign-in code expired or was already used. Run tourclaim login again.", ExitCode.AUTH, "expired_token");
      default:
        throw new CliError(
          sentences(`The sign-in failed (${err.error ?? `HTTP ${res.status}`})`, err.error_description),
          ExitCode.ERROR,
          "login_failed",
        );
    }
  }
}

async function deviceFlow(ctx: Context, scopes: string[], noBrowser: boolean): Promise<DeviceTokenSuccess> {
  const api = ctx.client();
  const body: { client: string; scopes?: string[] } = { client: ctx.userAgent };
  if (scopes.length) body.scopes = scopes;
  const { data: code } = await api.request<DeviceCodeResponse>(DEVICE_CODE_PATH, { method: "POST", body, auth: false });
  if (
    !code ||
    typeof code.device_code !== "string" ||
    typeof code.user_code !== "string" ||
    !isHttpUrl(code.verification_uri) ||
    typeof code.expires_in !== "number"
  ) {
    throw new CliError("The API returned an unexpected sign-in response.", ExitCode.ERROR, "bad_response");
  }
  const complete = isHttpUrl(code.verification_uri_complete) ? code.verification_uri_complete : null;
  if (ctx.json) {
    ctx.out.data({
      event: "device_code",
      user_code: code.user_code,
      verification_uri: code.verification_uri,
      verification_uri_complete: complete,
      expires_in: code.expires_in,
      interval: code.interval ?? 5,
    });
  } else {
    ctx.out.lines([
      "",
      "To connect this computer to your TourClaim account:",
      "",
      `  1. Open   ${code.verification_uri}`,
      `  2. Enter  ${code.user_code}`,
      "",
      "Only approve this if you started this sign-in yourself, and check that the code matches.",
    ]);
  }
  let opened = false;
  if (ctx.deps.stdoutIsTTY && !noBrowser) opened = await ctx.deps.openUrl(complete ?? code.verification_uri);
  if (!ctx.json) ctx.out.line(opened ? "Opened the page in your browser." : "");
  const minutes = Math.max(1, Math.round(code.expires_in / 60));
  ctx.out.info(`Waiting for approval (the code expires in ${minutes} minute${minutes === 1 ? "" : "s"}). Press Ctrl-C to cancel.`);
  return pollForToken(ctx, api, code);
}

async function readKey(ctx: Context): Promise<string> {
  const raw = ctx.deps.stdinIsTTY
    ? await ctx.deps.prompt("Paste the TourClaim API key (input is hidden): ", { hidden: true })
    : await ctx.deps.readStdin();
  const key = raw.trim();
  ctx.out.addSecret(key);
  if (!key) throw new UsageError("No key was given. Pipe it in: tourclaim login --with-token < key.txt");
  if (!KEY_FORMAT.test(key)) {
    throw new UsageError("That does not look like a TourClaim key. Keys start with tc_muse_ and contain only letters, digits, - and _.");
  }
  return key;
}

export const login: Command = {
  path: ["login"],
  summary: "Connect this computer to a traveler's TourClaim account",
  usage: "tourclaim login [--no-browser] [--scope <scope>]... [--force]\n       tourclaim login --with-token [--force] < key.txt",
  description: [
    "Signs in with a short code the traveler approves in their own browser, then saves a key for this API URL.",
    "The key belongs to one traveler, lasts 30 days and cannot be refreshed; sign in again when it expires.",
    "With --json, the first line carries the code and URL to show the traveler; the last line is the result.",
    "",
    "--with-token saves a key the traveler already created at /connect/muse. It reads the key from stdin",
    "(piped) or a hidden prompt. Keys are never accepted as command-line arguments.",
  ].join("\n"),
  options: {
    "with-token": { type: "boolean", description: "Read an existing key from stdin or a hidden prompt instead of the browser flow." },
    "no-browser": { type: "boolean", description: "Do not open the browser; just print the URL and code." },
    scope: {
      type: "string",
      multiple: true,
      value: "<scope>",
      description: `Ask only for these permissions (repeat or comma-separate): ${SCOPES.join(", ")}. Default: all.`,
    },
    force: {
      type: "boolean",
      description: "Replace a valid stored key with a new one and revoke the old key (a traveler can hold at most 5). Drafts stay reachable.",
    },
  },
  examples: ["tourclaim login", "tourclaim login --json --no-browser", "tourclaim login --with-token < key.txt"],
  async run(ctx, args) {
    if (args.positionals.length) {
      throw new UsageError(
        "tourclaim login takes no arguments. Never put a key on the command line (shell history and process lists keep it); pipe it instead: tourclaim login --with-token < key.txt",
      );
    }
    const withToken = flag(args, "with-token");
    const force = flag(args, "force");
    const scopes = parseScopes(strs(args, "scope"));
    if (withToken && scopes.length) throw new UsageError("--scope applies to the browser sign-in; a pasted key keeps the permissions it was created with.");
    if (ctx.deps.env.TOURCLAIM_API_KEY) {
      ctx.out.warn("TOURCLAIM_API_KEY is set. It overrides the stored key for every command until you unset it.");
    }

    const existing = await ctx.store.get(ctx.apiUrl);
    if (existing) ctx.out.addSecret(existing.api_key);
    const existingCheck = existing ? await keyInfo(ctx, existing.api_key) : null;
    if (existing && existingCheck && !force) {
      if (withToken) {
        throw new UsageError(
          `Already signed in to ${ctx.apiUrl}${existingCheck.info.account_email ? ` as ${existingCheck.info.account_email}` : ""}. Pass --force to replace the stored key; the old key is revoked.`,
        );
      }
      report(ctx, { info: existingCheck.info, mode: existingCheck.mode, credential: existing, replacedKeyId: null, already: true });
      return ExitCode.OK;
    }

    let credential: StoredCredential;
    let confirmed: { info: KeyInfo; mode: string | null } | null = null;
    if (withToken) {
      const key = await readKey(ctx);
      confirmed = await keyInfo(ctx, key);
      if (!confirmed) {
        throw new CliError("The API rejected that key (expired, revoked or mistyped). Nothing was saved.", ExitCode.AUTH, "unauthorized");
      }
      credential = { api_key: key, expires_at: confirmed.info.expires_at, grant_id: confirmed.info.id, scopes: confirmed.info.scopes };
    } else {
      const token = await deviceFlow(ctx, scopes, flag(args, "no-browser"));
      if (typeof token.api_key !== "string" || !KEY_FORMAT.test(token.api_key)) {
        throw new CliError("The API returned a key in an unexpected format. Nothing was saved.", ExitCode.ERROR, "bad_response");
      }
      ctx.out.addSecret(token.api_key);
      credential = {
        api_key: token.api_key,
        expires_at: typeof token.expires_at === "string" ? token.expires_at : null,
        grant_id: typeof token.grant_id === "string" ? token.grant_id : null,
        scopes: Array.isArray(token.scopes) ? token.scopes : [],
      };
    }

    await ctx.store.set(ctx.apiUrl, credential);
    ctx.forgetActiveKey();

    let replacedKeyId: string | null = null;
    if (existing && existingCheck && existing.api_key !== credential.api_key) {
      try {
        await ctx.client(existing.api_key).request(KEY_PATH, { method: "DELETE" });
        replacedKeyId = existingCheck.info.id;
      } catch (error) {
        ctx.out.warn(`Could not revoke the previous key (${(error as Error).message}). Revoke it at ${manageKeysUrl(ctx)}`);
      }
    }

    if (!confirmed) {
      try {
        confirmed = await keyInfo(ctx, credential.api_key);
      } catch (error) {
        ctx.out.warn(`Saved the key, but could not confirm it: ${(error as Error).message}`);
      }
    }
    report(ctx, { info: confirmed?.info ?? null, mode: confirmed?.mode ?? null, credential, replacedKeyId, already: false });
    return ExitCode.OK;
  },
};

export const logout: Command = {
  path: ["logout"],
  summary: "Revoke the key and remove it from this computer",
  usage: "tourclaim logout",
  description:
    "Revokes the key in use on the server, then deletes the stored copy (even if the server already considers it invalid).\nUnsubmitted drafts started with tourclaim login stay reachable the next time you run tourclaim login; submitted claims are not affected.",
  maxArgs: 0,
  async run(ctx) {
    const active = await ctx.activeKey();
    if (!active) {
      if (ctx.json) ctx.out.data({ api_url: ctx.apiUrl, revoked: false, already_invalid: false, credential_removed: false, key_source: null });
      else ctx.out.line(`Not signed in to ${ctx.apiUrl}; nothing to do.`);
      return ExitCode.OK;
    }
    let revoked = false;
    let alreadyInvalid = false;
    try {
      await ctx.client(active.key).request(KEY_PATH, { method: "DELETE" });
      revoked = true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        alreadyInvalid = true;
      } else if (error instanceof CliError) {
        throw new CliError(
          sentences(error.message, `The key was not revoked and is still stored. Try again, or revoke it at ${manageKeysUrl(ctx)}`),
          error.exitCode,
          error.code,
          error.extra,
        );
      } else {
        throw error;
      }
    }
    let removed = false;
    if (active.stored && (active.source === "stored" || active.stored.api_key === active.key)) {
      removed = await ctx.store.remove(ctx.apiUrl);
    }
    if (ctx.json) {
      ctx.out.data({ api_url: ctx.apiUrl, revoked, already_invalid: alreadyInvalid, credential_removed: removed, key_source: active.source });
    } else {
      ctx.out.line(revoked ? `Revoked the key for ${ctx.apiUrl}.` : "The key was already expired or revoked.");
      if (removed) ctx.out.line(`Removed it from ${ctx.store.path}.`);
    }
    if (active.source === "env") {
      ctx.out.info("TOURCLAIM_API_KEY is still set in this environment; unset it.");
      if (active.stored && !removed) {
        ctx.out.info(`A different key is still stored for ${ctx.apiUrl}. Unset TOURCLAIM_API_KEY and run tourclaim logout again to revoke it too.`);
      }
    }
    return ExitCode.OK;
  },
};

export const status: Command = {
  path: ["status"],
  aliases: [["whoami"]],
  summary: "Show the API mode and who is signed in",
  usage: "tourclaim status",
  description:
    "Shows whether the connector is enabled, whether it runs in review mode (synthetic claims) or live, and the signed-in traveler and key expiry.\nExits 0 when signed in, 3 when not signed in or the key is rejected, 7 when the connector is disabled.",
  maxArgs: 0,
  async run(ctx) {
    const { data: info } = await ctx.client().request<ConnectorInfo>(API_PREFIX, { auth: false });
    const active = await ctx.activeKey();
    let key: KeyInfo | null = null;
    let keyProblem: string | null = null;
    if (active) {
      try {
        key = (await (await ctx.authed()).request<KeyInfo>(KEY_PATH)).data;
      } catch (error) {
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) keyProblem = "rejected";
        else if (error instanceof ApiError && error.status === 503) keyProblem = "connector_unavailable";
        else throw error;
      }
    }
    const signedIn = key !== null;
    const connectionUrl = absoluteUrl(ctx.apiUrl, info.connection_url || "/connect/muse");
    if (ctx.json) {
      ctx.out.data({
        api_url: ctx.apiUrl,
        mode: info.mode,
        enabled: info.enabled,
        connector: {
          name: info.name,
          version: info.version,
          enabled: info.enabled,
          mode: info.mode,
          openapi_url: absoluteUrl(ctx.apiUrl, info.openapi_url),
          connection_url: connectionUrl,
          documentation_url: absoluteUrl(ctx.apiUrl, info.documentation_url),
          cli_login_url: absoluteUrl(ctx.apiUrl, info.cli_login_url || "/connect/cli"),
        },
        signed_in: signedIn,
        account_email: key?.account_email ?? null,
        key: key ? { id: key.id, expires_at: key.expires_at, scopes: key.scopes } : null,
        key_source: active?.source ?? null,
        key_problem: keyProblem,
        credentials_path: ctx.store.path,
      });
    } else {
      const notice = modeNotice(info.mode);
      if (notice) ctx.out.lines([notice.toUpperCase(), ""]);
      const pad = (label: string) => label.padEnd(14);
      ctx.out.line(pad("API:") + ctx.apiUrl);
      ctx.out.line(pad("Connector:") + `${info.enabled ? "enabled" : "disabled"} (${info.name} ${info.version})`);
      ctx.out.line(pad("Mode:") + info.mode);
      if (signedIn && key) {
        ctx.out.line(pad("Signed in:") + `yes${key.account_email ? `, as ${key.account_email}` : ""}`);
        ctx.out.line(pad("Key:") + `${key.id}, expires ${formatTime(key.expires_at)} (${relativeTime(key.expires_at, ctx.deps.now())})`);
        ctx.out.line(pad("Permissions:") + (key.scopes.join(", ") || "none"));
        ctx.out.line(pad("Key source:") + (active?.source === "env" ? "TOURCLAIM_API_KEY" : ctx.store.path));
      } else if (keyProblem === "rejected") {
        ctx.out.line(pad("Signed in:") + "no: the key was rejected (expired or revoked). Run: tourclaim login");
      } else if (keyProblem) {
        ctx.out.line(pad("Signed in:") + "unknown: the connector is unavailable");
      } else {
        ctx.out.line(pad("Signed in:") + "no. Run: tourclaim login");
      }
      ctx.out.line(pad("Manage keys:") + connectionUrl);
    }
    if (!info.enabled) return ExitCode.UNAVAILABLE;
    return signedIn ? ExitCode.OK : keyProblem === "connector_unavailable" ? ExitCode.UNAVAILABLE : ExitCode.AUTH;
  },
};
