/** Process exit codes. Documented in README.md; keep them stable. */
export const ExitCode = {
  OK: 0,
  ERROR: 1,
  USAGE: 2,
  AUTH: 3,
  CONFLICT: 4,
  RATE_LIMITED: 5,
  NOT_FOUND: 6,
  UNAVAILABLE: 7,
} as const;

export type ExitCodeValue = (typeof ExitCode)[keyof typeof ExitCode];

/**
 * An error the CLI reports to the user and turns into an exit code.
 * `code` is a stable machine-readable name used in --json error output.
 */
export class CliError extends Error {
  readonly exitCode: number;
  readonly code: string;
  readonly extra: Record<string, unknown>;

  constructor(message: string, exitCode: number = ExitCode.ERROR, code = "error", extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "CliError";
    this.exitCode = exitCode;
    this.code = code;
    this.extra = extra;
  }
}

export class UsageError extends CliError {
  constructor(message: string, extra: Record<string, unknown> = {}) {
    super(message, ExitCode.USAGE, "usage_error", extra);
    this.name = "UsageError";
  }
}
