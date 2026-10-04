import { CliError, ExitCode } from "./errors.js";
import type { ValidationIssue } from "./types.js";

export const API_PREFIX = "/api/connectors/v1";
const MAX_RETRY_AFTER_SECONDS = 60;
const DEFAULT_RETRY_AFTER_SECONDS = 5;
const REQUEST_TIMEOUT_MS = 90_000;

export interface ApiResponse<T> {
  status: number;
  data: T;
  headers: Headers;
}

/** An HTTP error from the API, already mapped to an exit code. */
export class ApiError extends CliError {
  readonly status: number;
  readonly detail: unknown;
  readonly body: unknown;

  constructor(status: number, message: string, exitCode: number, code: string, body: unknown, extra: Record<string, unknown> = {}) {
    super(message, exitCode, code, { status, ...extra });
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : undefined;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  query?: Record<string, string | number | undefined>;
  /** Send the bearer key. Default true. */
  auth?: boolean;
  /** Statuses returned to the caller instead of thrown. */
  allowStatus?: number[];
  /** Retry once after a 429 that asks for at most 60 seconds. Default true. */
  retryRateLimit?: boolean;
}

export interface ApiClientOptions {
  baseUrl: string;
  apiKey: string | null;
  userAgent: string;
  fetch: typeof fetch;
  sleep: (ms: number) => Promise<void>;
  /** Progress messages, such as a rate-limit wait. */
  notice: (message: string) => void;
  /** Called when a request needs a key and there is none. */
  missingKey: () => CliError;
}

export class ApiClient {
  constructor(private readonly options: ApiClientOptions) {}

  get baseUrl(): string {
    return this.options.baseUrl;
  }

  url(path: string, query?: RequestOptions["query"]): string {
    const url = new URL(this.options.baseUrl + path);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    return url.href;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
    const method = options.method ?? "GET";
    const headers: Record<string, string> = {
      "User-Agent": this.options.userAgent,
      Accept: "application/json",
      ...options.headers,
    };
    if (options.auth !== false) {
      if (!this.options.apiKey) throw this.options.missingKey();
      headers.Authorization = `Bearer ${this.options.apiKey}`;
    }
    let body: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    const url = this.url(path, options.query);

    for (let attempt = 0; ; attempt++) {
      let response: Response;
      try {
        response = await this.options.fetch(url, {
          method,
          headers,
          body,
          redirect: "manual",
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
      } catch (error) {
        throw networkError(this.options.baseUrl, error);
      }
      const text = await response.text().catch(() => "");
      const data = parseJson(text);
      const allowed = options.allowStatus?.includes(response.status) ?? false;

      if (response.status === 429 && !allowed && attempt === 0 && options.retryRateLimit !== false) {
        const wait = retryAfterSeconds(response.headers.get("retry-after"));
        if (wait <= MAX_RETRY_AFTER_SECONDS) {
          this.options.notice(`Rate limited by the API; retrying in ${wait} second${wait === 1 ? "" : "s"}.`);
          await this.options.sleep(wait * 1000);
          continue;
        }
      }
      if ((response.status >= 200 && response.status < 300) || allowed) {
        return { status: response.status, data: data as T, headers: response.headers };
      }
      throw toApiError(response.status, data, response.headers);
    }
  }
}

function parseJson(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Seconds from a Retry-After header (delta-seconds or an HTTP date). */
export function retryAfterSeconds(value: string | null, now = Date.now()): number {
  if (!value) return DEFAULT_RETRY_AFTER_SECONDS;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (!Number.isNaN(date)) return Math.max(0, Math.ceil((date - now) / 1000));
  return DEFAULT_RETRY_AFTER_SECONDS;
}

function networkError(baseUrl: string, error: unknown): CliError {
  const err = error as Error & { cause?: { code?: string; message?: string } };
  if (err?.name === "TimeoutError" || err?.name === "AbortError") {
    return new CliError(`The request to ${baseUrl} timed out.`, ExitCode.ERROR, "timeout");
  }
  const reason = err?.cause?.code ?? err?.cause?.message ?? err?.message ?? String(error);
  return new CliError(`Could not reach ${baseUrl} (${reason}).`, ExitCode.ERROR, "network_error");
}

/** Readable text for a validation issue's location, such as fields.trip_date. */
export function issueLocation(issue: ValidationIssue): string {
  const parts = Array.isArray(issue.loc) ? issue.loc.map(String) : [];
  if (parts[0] === "body" || parts[0] === "query" || parts[0] === "path") parts.shift();
  return parts.join(".") || "request";
}

/** A sentence describing an error body of any of the API's shapes. */
export function describeErrorBody(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    if (typeof b.detail === "string" && b.detail) return b.detail;
    if (Array.isArray(b.detail)) {
      const issues = (b.detail as ValidationIssue[]).map((issue) => {
        const where = issueLocation(issue);
        return issue.type ? `${where} (${issue.type})` : where;
      });
      return issues.length ? `The API rejected these values: ${issues.join(", ")}.` : "The API rejected the request as invalid.";
    }
    if (typeof b.error_description === "string" && b.error_description) return b.error_description;
    if (typeof b.error === "string" && b.error) return b.error;
  }
  return `The API answered HTTP ${status}.`;
}

/** Joins sentences, adding a full stop where one is missing. */
export function sentences(...parts: Array<string | undefined | null>): string {
  return parts
    .map((part) => (part ?? "").trim())
    .filter(Boolean)
    .map((part) => (/[.!?:)]$/.test(part) ? part : `${part}.`))
    .join(" ");
}

export function toApiError(status: number, body: unknown, headers: Headers): ApiError {
  const message = describeErrorBody(status, body);
  const json = typeof body === "string" ? null : body;
  if (status >= 300 && status < 400) {
    const location = headers.get("location");
    return new ApiError(status, `The API redirected${location ? ` to ${location}` : ""}. Check --api-url.`, ExitCode.ERROR, "redirect", json);
  }
  switch (status) {
    case 401:
      return new ApiError(status, sentences(message, "The key is missing, expired or revoked. Run: tourclaim login"), ExitCode.AUTH, "unauthorized", json);
    case 403:
      return new ApiError(status, sentences(message, "This key was not granted permission for this action. Run tourclaim login --force to connect again with the permission it needs"), ExitCode.AUTH, "forbidden", json);
    case 404:
      return new ApiError(status, message, ExitCode.NOT_FOUND, "not_found", json);
    case 409:
      return new ApiError(status, message, ExitCode.CONFLICT, "conflict", json);
    case 413:
      return new ApiError(status, sentences(message, "The request was too large; files may be at most 5 MiB"), ExitCode.ERROR, "too_large", json);
    case 422:
      return new ApiError(status, message, ExitCode.ERROR, "invalid", json);
    case 429: {
      const retryAfter = retryAfterSeconds(headers.get("retry-after"));
      return new ApiError(status, sentences(message, `Try again in ${retryAfter} seconds`), ExitCode.RATE_LIMITED, "rate_limited", json, { retry_after: retryAfter });
    }
    case 503:
      return new ApiError(status, sentences(message, "The TourClaim connector is unavailable right now; nothing was changed. Try again later"), ExitCode.UNAVAILABLE, "unavailable", json);
    default:
      return new ApiError(status, message, ExitCode.ERROR, "api_error", json);
  }
}
