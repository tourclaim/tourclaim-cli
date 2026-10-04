import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { createInterface } from "node:readline";
import { CliError } from "./errors.js";

/**
 * Everything the CLI touches outside its own code. Tests pass fakes; the
 * real implementations live in realDeps().
 */
export interface Deps {
  env: Record<string, string | undefined>;
  platform: NodeJS.Platform;
  arch: string;
  nodeVersion: string;
  homedir: string;
  stdout: (text: string) => void;
  stderr: (text: string) => void;
  stdinIsTTY: boolean;
  stdoutIsTTY: boolean;
  /** Reads all of stdin as UTF-8. Only used when stdin is not a terminal. */
  readStdin: () => Promise<string>;
  /** Asks a question on stderr and reads one line from the terminal. */
  prompt: (question: string, options?: { hidden?: boolean }) => Promise<string>;
  /** Opens an http(s) URL in the default browser. Resolves false if it could not. */
  openUrl: (url: string) => Promise<boolean>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  fetch: typeof fetch;
}

export function realDeps(): Deps {
  return {
    env: process.env,
    platform: process.platform,
    arch: process.arch,
    nodeVersion: process.versions.node,
    homedir: homedir(),
    stdout: (text) => {
      process.stdout.write(text);
    },
    stderr: (text) => {
      process.stderr.write(text);
    },
    stdinIsTTY: Boolean(process.stdin.isTTY),
    stdoutIsTTY: Boolean(process.stdout.isTTY),
    readStdin,
    prompt: (question, options) => (options?.hidden ? hiddenPrompt(question) : linePrompt(question)),
    openUrl: (url) => openUrl(url, process.platform, process.env),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    now: () => Date.now(),
    fetch: (input, init) => fetch(input, init),
  };
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : (chunk as Buffer));
  }
  return Buffer.concat(chunks).toString("utf8");
}

function linePrompt(question: string): Promise<string> {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stderr, terminal: true });
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer);
    });
  });
}

/** Reads a line from the terminal without echoing it. */
function hiddenPrompt(question: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const stdin = process.stdin;
    process.stderr.write(question);
    let value = "";
    const finish = () => {
      stdin.off("data", onData);
      stdin.setRawMode(false);
      stdin.pause();
      process.stderr.write("\n");
    };
    const onData = (chunk: string) => {
      for (const ch of chunk) {
        if (ch === "\r" || ch === "\n") {
          finish();
          resolve(value);
          return;
        }
        if (ch === "\u0003") {
          finish();
          reject(new CliError("Cancelled.", 130, "cancelled"));
          return;
        }
        if (ch === "\u0004" && value === "") {
          finish();
          resolve(value);
          return;
        }
        if (ch === "\u007f" || ch === "\b") {
          value = value.slice(0, -1);
          continue;
        }
        if (ch >= " ") value += ch;
      }
    };
    stdin.setRawMode(true);
    stdin.setEncoding("utf8");
    stdin.resume();
    stdin.on("data", onData);
  });
}

/** Opens a URL with the platform's default handler. Never runs a shell. */
export function openUrl(url: string, platform: NodeJS.Platform, env: Record<string, string | undefined>): Promise<boolean> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return Promise.resolve(false);
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return Promise.resolve(false);
  let command: string;
  let args: string[];
  if (platform === "darwin") {
    command = "open";
    args = [parsed.href];
  } else if (platform === "win32") {
    command = "rundll32";
    args = ["url.dll,FileProtocolHandler", parsed.href];
  } else {
    // No display (SSH session, container): do not try.
    if (!env.DISPLAY && !env.WAYLAND_DISPLAY) return Promise.resolve(false);
    command = "xdg-open";
    args = [parsed.href];
  }
  return new Promise((resolve) => {
    try {
      const child = spawn(command, args, { stdio: "ignore", detached: true });
      child.once("error", () => resolve(false));
      child.once("spawn", () => {
        child.unref();
        resolve(true);
      });
    } catch {
      resolve(false);
    }
  });
}
