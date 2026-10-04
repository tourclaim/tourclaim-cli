import type { Context } from "./context.js";
import { UsageError } from "./errors.js";

export interface OptionSpec {
  type: "string" | "boolean";
  short?: string;
  multiple?: boolean;
  /** Placeholder shown in help, such as <file>. */
  value?: string;
  description: string;
}

export interface Parsed {
  positionals: string[];
  values: Record<string, string | boolean | string[] | boolean[] | undefined>;
}

export interface Command {
  /** Words that select the command, such as ["intake", "set"]. */
  path: string[];
  aliases?: string[][];
  summary: string;
  usage: string;
  description?: string;
  options?: Record<string, OptionSpec>;
  examples?: string[];
  minArgs?: number;
  maxArgs?: number;
  run(ctx: Context, args: Parsed): Promise<number | void>;
}

export const GLOBAL_OPTIONS: Record<string, OptionSpec> = {
  json: { type: "boolean", description: "Machine-readable output: one JSON value per line on stdout; errors as JSON on stderr." },
  "api-url": { type: "string", value: "<url>", description: "API base URL (default https://app.getcopernican.com, or TOURCLAIM_API_URL)." },
  help: { type: "boolean", short: "h", description: "Show help." },
  version: { type: "boolean", description: "Print the version." },
};

export function str(args: Parsed, name: string): string | undefined {
  const v = args.values[name];
  return typeof v === "string" ? v : undefined;
}

export function strs(args: Parsed, name: string): string[] {
  const v = args.values[name];
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === "string");
  return typeof v === "string" ? [v] : [];
}

export function flag(args: Parsed, name: string): boolean {
  return args.values[name] === true;
}

export function intArg(args: Parsed, name: string, min = 0): number | undefined {
  const v = str(args, name);
  if (v === undefined) return undefined;
  if (!/^\d+$/.test(v) || Number(v) < min) throw new UsageError(`--${name} must be a whole number${min > 0 ? ` of at least ${min}` : ""}.`);
  return Number(v);
}
