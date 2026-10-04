import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Deps } from "../src/deps.js";
import { run } from "../src/main.js";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const CLI = join(ROOT, "dist", "cli.js");

export interface CliResult {
  code: number;
  stdout: string;
  stderr: string;
  /** Every sleep the CLI asked for, in milliseconds. */
  sleeps: number[];
  /** URLs the CLI tried to open in a browser. */
  opened: string[];
  /** Questions the CLI asked on the terminal. */
  prompts: string[];
  /** The last JSON line on stdout. */
  json: () => any;
  /** Every JSON line on stdout. */
  jsonLines: () => any[];
  /** The JSON error object written to stderr in --json mode. */
  jsonError: () => any;
}

export interface CliOptions {
  home: string;
  apiUrl?: string;
  env?: Record<string, string | undefined>;
  stdin?: string;
  stdinIsTTY?: boolean;
  stdoutIsTTY?: boolean;
  /** Answers to prompts, in order. */
  answers?: string[];
  now?: number;
  openSucceeds?: boolean;
  /** Called on each sleep, after the fake clock advances. */
  onSleep?: (ms: number) => void | Promise<void>;
}

function parseLines(text: string): any[] {
  return text
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

export function result(code: number, stdout: string, stderr: string, extra: Partial<CliResult> = {}): CliResult {
  return {
    code,
    stdout,
    stderr,
    sleeps: [],
    opened: [],
    prompts: [],
    json: () => {
      const lines = parseLines(stdout);
      return lines[lines.length - 1];
    },
    jsonLines: () => parseLines(stdout),
    jsonError: () => {
      const lines = parseLines(stderr.split("\n").filter((l) => l.startsWith("{")).join("\n"));
      return lines[lines.length - 1]?.error;
    },
    ...extra,
  };
}

/** Runs the CLI in-process with a fake clock, terminal and browser. */
export async function runCli(argv: string[], options: CliOptions): Promise<CliResult> {
  let stdout = "";
  let stderr = "";
  const sleeps: number[] = [];
  const opened: string[] = [];
  const prompts: string[] = [];
  const answers = [...(options.answers ?? [])];
  let clock = options.now ?? Date.now();
  const deps: Deps = {
    env: {
      XDG_CONFIG_HOME: join(options.home, ".config"),
      ...(options.apiUrl ? { TOURCLAIM_API_URL: options.apiUrl } : {}),
      ...options.env,
    },
    platform: process.platform === "win32" ? "linux" : process.platform,
    arch: "x64",
    nodeVersion: "20.0.0",
    homedir: options.home,
    stdout: (t) => {
      stdout += t;
    },
    stderr: (t) => {
      stderr += t;
    },
    stdinIsTTY: options.stdinIsTTY ?? false,
    stdoutIsTTY: options.stdoutIsTTY ?? false,
    readStdin: async () => options.stdin ?? "",
    prompt: async (question) => {
      prompts.push(question);
      const answer = answers.shift();
      if (answer === undefined) throw new Error(`unexpected prompt: ${question}`);
      return answer;
    },
    openUrl: async (url) => {
      opened.push(url);
      return options.openSucceeds ?? true;
    },
    sleep: async (ms) => {
      sleeps.push(ms);
      clock += ms;
      await options.onSleep?.(ms);
    },
    now: () => clock,
    fetch: (input, init) => fetch(input, init),
  };
  const code = await run(argv, deps);
  return result(code, stdout, stderr, { sleeps, opened, prompts });
}

export async function tempHome(): Promise<{ home: string; cleanup: () => Promise<void> }> {
  const home = await mkdtemp(join(tmpdir(), "tourclaim-test-"));
  return { home, cleanup: () => rm(home, { recursive: true, force: true }) };
}

export interface Spawned {
  /** Resolves with the result once the process exits. */
  done: Promise<CliResult>;
  /** Resolves with the first stdout line. */
  firstLine: Promise<string>;
  write: (text: string) => void;
}

/** Runs the built dist/cli.js as a child process. Never blocks the event loop. */
export function spawnCli(argv: string[], options: { home: string; env?: Record<string, string>; stdin?: string }): Spawned {
  const child = spawn(process.execPath, [CLI, ...argv], {
    env: {
      PATH: process.env.PATH ?? "",
      HOME: options.home,
      XDG_CONFIG_HOME: join(options.home, ".config"),
      ...options.env,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  let resolveLine: (line: string) => void = () => {};
  const firstLine = new Promise<string>((resolve) => {
    resolveLine = resolve;
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    stdout += chunk;
    const nl = stdout.indexOf("\n");
    if (nl !== -1) resolveLine(stdout.slice(0, nl));
  });
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });
  if (options.stdin !== undefined) child.stdin.end(options.stdin);
  const done = new Promise<CliResult>((resolve, reject) => {
    child.on("error", reject);
    child.on("close", (code) => {
      resolveLine(stdout);
      resolve(result(code ?? -1, stdout, stderr));
    });
  });
  return { done, firstLine, write: (text) => child.stdin.write(text) };
}
