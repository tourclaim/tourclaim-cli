import { UsageError } from "./errors.js";
import { VERSION } from "./version.js";

export const DEFAULT_API_URL = "https://app.getcopernican.com";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * Resolves the API base URL: --api-url, then TOURCLAIM_API_URL, then the
 * default. The result is an origin plus optional path prefix, with no
 * trailing slash. It is also the key credentials are stored under.
 */
export function resolveApiUrl(flag: string | undefined, env: Record<string, string | undefined>): string {
  const raw = flag ?? env.TOURCLAIM_API_URL ?? DEFAULT_API_URL;
  return normalizeApiUrl(raw, flag !== undefined ? "--api-url" : env.TOURCLAIM_API_URL ? "TOURCLAIM_API_URL" : "default");
}

export function normalizeApiUrl(raw: string, source = "--api-url"): string {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new UsageError(`${source} is not a valid URL.`);
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new UsageError(`${source} must be an https:// URL.`);
  }
  if (url.username || url.password) {
    throw new UsageError(`${source} must not contain a user name or password.`);
  }
  if (url.protocol === "http:" && !LOOPBACK_HOSTS.has(url.hostname)) {
    throw new UsageError(`${source} uses plain http for ${url.hostname}. Keys are only sent over https (plain http is allowed for localhost).`);
  }
  let path = url.pathname.replace(/\/+$/, "");
  // Accept the API root itself, which people copy from the docs.
  path = path.replace(/\/api\/connectors(\/v1)?$/, "");
  return url.origin + path;
}

export function userAgent(platform: string, arch: string, nodeVersion: string): string {
  return `tourclaim-cli/${VERSION} (${platform} ${arch}; node ${nodeVersion})`;
}

/** Absolute URL for a path the API returned relative to its origin. */
export function absoluteUrl(apiUrl: string, pathOrUrl: string): string {
  try {
    return new URL(pathOrUrl, apiUrl + "/").href;
  } catch {
    return pathOrUrl;
  }
}
