import { randomBytes } from "node:crypto";
import { promises as fs } from "node:fs";
import { dirname, join, posix, win32 } from "node:path";
import { CliError } from "./errors.js";

/** What is saved per API base URL. */
export interface StoredCredential {
  api_key: string;
  expires_at: string | null;
  grant_id: string | null;
  scopes: string[];
}

type CredentialFile = Record<string, StoredCredential>;

/**
 * $XDG_CONFIG_HOME/tourclaim/credentials.json (default ~/.config), or
 * %APPDATA%\tourclaim\credentials.json on Windows.
 */
export function credentialsPath(env: Record<string, string | undefined>, platform: NodeJS.Platform, home: string): string {
  if (platform === "win32") {
    const appData = env.APPDATA && win32.isAbsolute(env.APPDATA) ? env.APPDATA : win32.join(home, "AppData", "Roaming");
    return win32.join(appData, "tourclaim", "credentials.json");
  }
  const xdg = env.XDG_CONFIG_HOME && posix.isAbsolute(env.XDG_CONFIG_HOME) ? env.XDG_CONFIG_HOME : posix.join(home, ".config");
  return posix.join(xdg, "tourclaim", "credentials.json");
}

async function readFile(path: string, platform: NodeJS.Platform, warn: (msg: string) => void): Promise<CredentialFile> {
  let text: string;
  try {
    if (platform !== "win32") {
      const info = await fs.stat(path);
      if ((info.mode & 0o077) !== 0) {
        warn(`${path} can be read by other users on this computer. Run: chmod 600 "${path}"`);
      }
    }
    text = await fs.readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw new CliError(`Could not read ${path}: ${(error as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new CliError(`${path} is not valid JSON. Delete it and run: tourclaim login`, 1, "bad_credentials_file");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new CliError(`${path} has an unexpected format. Delete it and run: tourclaim login`, 1, "bad_credentials_file");
  }
  const out: CredentialFile = {};
  for (const [url, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (!value || typeof value !== "object") continue;
    const v = value as Record<string, unknown>;
    if (typeof v.api_key !== "string" || !v.api_key) continue;
    out[url] = {
      api_key: v.api_key,
      expires_at: typeof v.expires_at === "string" ? v.expires_at : null,
      grant_id: typeof v.grant_id === "string" ? v.grant_id : null,
      scopes: Array.isArray(v.scopes) ? v.scopes.filter((s): s is string => typeof s === "string") : [],
    };
  }
  return out;
}

async function writeFile(path: string, platform: NodeJS.Platform, data: CredentialFile): Promise<void> {
  const dir = dirname(path);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  if (platform !== "win32") await fs.chmod(dir, 0o700);
  if (Object.keys(data).length === 0) {
    await fs.rm(path, { force: true });
    return;
  }
  // Write a new file readable only by this user, then move it into place.
  const tmp = join(dir, `.credentials.${process.pid}.${randomBytes(6).toString("hex")}.tmp`);
  try {
    await fs.writeFile(tmp, JSON.stringify(data, null, 2) + "\n", { mode: 0o600, flag: "wx" });
    if (platform !== "win32") await fs.chmod(tmp, 0o600);
    await fs.rename(tmp, path);
  } catch (error) {
    await fs.rm(tmp, { force: true });
    throw new CliError(`Could not save credentials to ${path}: ${(error as Error).message}`);
  }
}

export class CredentialStore {
  constructor(
    readonly path: string,
    private readonly platform: NodeJS.Platform,
    private readonly warn: (msg: string) => void,
  ) {}

  async get(apiUrl: string): Promise<StoredCredential | null> {
    const all = await readFile(this.path, this.platform, this.warn);
    return all[apiUrl] ?? null;
  }

  async set(apiUrl: string, credential: StoredCredential): Promise<void> {
    const all = await readFile(this.path, this.platform, () => {});
    all[apiUrl] = credential;
    await writeFile(this.path, this.platform, all);
  }

  /** Returns true if a credential was removed. */
  async remove(apiUrl: string): Promise<boolean> {
    const all = await readFile(this.path, this.platform, () => {});
    if (!(apiUrl in all)) return false;
    delete all[apiUrl];
    await writeFile(this.path, this.platform, all);
    return true;
  }
}
