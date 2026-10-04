import { ApiError } from "./api.js";
import { CliError, ExitCode } from "./errors.js";

const KEY_PATTERN = /tc_muse_[A-Za-z0-9_-]+/g;

/**
 * Writes to stdout and stderr. Every write passes through redact(), so a key
 * can never reach the terminal or a log even if an error message carried one.
 */
export class Output {
  private secrets: string[] = [];

  constructor(
    private readonly writeOut: (text: string) => void,
    private readonly writeErr: (text: string) => void,
    public json: boolean,
  ) {}

  /** Adds a value that must never be printed (for keys without the usual prefix). */
  addSecret(value: string | null | undefined): void {
    if (value && value.length >= 8 && !this.secrets.includes(value)) this.secrets.push(value);
  }

  redact(text: string): string {
    let out = text.replace(KEY_PATTERN, "tc_muse_[redacted]");
    for (const secret of this.secrets) out = out.split(secret).join("[redacted]");
    return out;
  }

  /** A line of human output on stdout. Ignored in --json mode. */
  line(text = ""): void {
    if (!this.json) this.writeOut(this.redact(text) + "\n");
  }

  /** Several lines of human output. */
  lines(lines: string[]): void {
    for (const l of lines) this.line(l);
  }

  /** One JSON value on its own line on stdout (--json mode). */
  data(value: unknown): void {
    this.writeOut(this.redact(JSON.stringify(value)) + "\n");
  }

  /** Progress or a notice, on stderr, in both modes. */
  info(text: string): void {
    this.writeErr(this.redact(text) + "\n");
  }

  warn(text: string): void {
    this.writeErr(this.redact(`warning: ${text}`) + "\n");
  }

  /** Raw text on stdout, used for help and the schema. */
  raw(text: string): void {
    this.writeOut(this.redact(text));
  }

  /** Reports an error and returns the exit code for it. */
  error(error: unknown): number {
    const err =
      error instanceof CliError
        ? error
        : new CliError(error instanceof Error ? error.message : String(error), ExitCode.ERROR, "internal_error");
    if (this.json) {
      const body: Record<string, unknown> = { code: err.code, message: err.message, exit_code: err.exitCode, ...err.extra };
      if (err instanceof ApiError && err.detail !== undefined) body.detail = err.detail;
      this.writeErr(this.redact(JSON.stringify({ error: body })) + "\n");
    } else {
      this.writeErr(this.redact(`error: ${err.message}`) + "\n");
      if (err instanceof ApiError && Array.isArray(err.detail)) {
        for (const issue of err.detail as Array<{ loc?: unknown[]; msg?: string; type?: string }>) {
          const where = Array.isArray(issue.loc) ? issue.loc.filter((p) => p !== "body").join(".") : "request";
          this.writeErr(this.redact(`  ${where}: ${issue.msg ?? "invalid"}${issue.type ? ` (${issue.type})` : ""}`) + "\n");
        }
      }
    }
    return err.exitCode;
  }
}
