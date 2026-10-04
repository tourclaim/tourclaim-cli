import { parseArgs } from "node:util";
import { GLOBAL_OPTIONS, type Command, type OptionSpec, type Parsed } from "./command.js";
import { login, logout, status } from "./commands/auth.js";
import { cardsSearch } from "./commands/cards.js";
import { claimsList, claimsShow } from "./commands/claims.js";
import {
  intakeAddEmail,
  intakeAttach,
  intakeDelete,
  intakeSet,
  intakeShow,
  intakeSign,
  intakeStart,
  intakeSubmit,
} from "./commands/intake.js";
import { schema } from "./commands/schema.js";
import { resolveApiUrl } from "./config.js";
import { Context } from "./context.js";
import type { Deps } from "./deps.js";
import { ExitCode, UsageError } from "./errors.js";
import { Output } from "./output.js";
import { VERSION } from "./version.js";

export const COMMANDS: Command[] = [
  login,
  logout,
  status,
  cardsSearch,
  intakeStart,
  intakeShow,
  intakeSet,
  intakeAttach,
  intakeAddEmail,
  intakeSign,
  intakeSubmit,
  intakeDelete,
  claimsList,
  claimsShow,
  schema,
];

const GROUPS: Record<string, string> = {
  cards: "Look up the card a booking was paid with",
  intake: "Start, fill in, sign and submit a claim draft",
  claims: "Check submitted claims",
};

function optionLines(options: Record<string, OptionSpec>): string[] {
  const entries = Object.entries(options).map(([name, spec]) => {
    const left = `${spec.short ? `-${spec.short}, ` : "    "}--${name}${spec.value ? ` ${spec.value}` : ""}`;
    return [left, spec.description] as const;
  });
  const width = Math.max(...entries.map(([left]) => left.length)) + 2;
  return entries.map(([left, desc]) => `  ${left.padEnd(width)}${desc}`);
}

function commandName(cmd: Command): string {
  const alias = cmd.aliases?.length ? ` (${cmd.aliases.map((a) => a.join(" ")).join(", ")})` : "";
  return cmd.path.join(" ") + alias;
}

function commandList(commands: Command[], strip = 0, extra: Array<[string, string]> = []): string[] {
  const rows: Array<[string, string]> = [
    ...commands.map((c): [string, string] => [commandName({ ...c, path: c.path.slice(strip) }), c.summary]),
    ...extra,
  ];
  const width = Math.max(...rows.map(([name]) => name.length)) + 2;
  return rows.map(([name, summary]) => `  ${name.padEnd(width)}${summary}`);
}

export function topHelp(): string {
  return [
    `tourclaim ${VERSION}: file a trip cancellation claim against the travel benefits of the credit card it was booked with,`,
    "through TourClaim by Copernican.",
    "",
    "Usage: tourclaim <command> [options]",
    "",
    "Commands:",
    ...commandList(COMMANDS, 0, [["help [command]", "Show help for a command"]]),
    "",
    "Global options:",
    ...optionLines(GLOBAL_OPTIONS),
    "",
    "Environment:",
    "  TOURCLAIM_API_URL     API base URL (default https://app.getcopernican.com)",
    "  TOURCLAIM_API_KEY     A key to use instead of the stored one (it takes precedence)",
    "  XDG_CONFIG_HOME       Credentials go in $XDG_CONFIG_HOME/tourclaim (default ~/.config; %APPDATA% on Windows)",
    "",
    "Exit codes:",
    "  0 ok  1 error  2 usage  3 not signed in or key rejected  4 conflict",
    "  5 rate limited  6 not found  7 connector disabled or unavailable",
    "",
    "Run tourclaim status to see whether the API is in review mode (synthetic claims, nothing filed) or live.",
    "Only the traveler can sign a claim authorization, in their own browser; this tool never signs.",
    "Run tourclaim <command> --help for details.",
    "",
  ].join("\n");
}

function groupHelp(group: string): string {
  const commands = COMMANDS.filter((c) => c.path[0] === group);
  return [
    `Usage: tourclaim ${group} <command> [options]`,
    "",
    `${GROUPS[group] ?? ""}.`,
    "",
    "Commands:",
    ...commandList(commands, 1),
    "",
    `Run tourclaim ${group} <command> --help for details.`,
    "",
  ].join("\n");
}

export function commandHelp(cmd: Command): string {
  const lines = [`Usage: ${cmd.usage}`, "", `${cmd.summary}.`];
  if (cmd.description) lines.push("", cmd.description);
  if (cmd.options && Object.keys(cmd.options).length) lines.push("", "Options:", ...optionLines(cmd.options));
  lines.push("", "Global options:", ...optionLines(GLOBAL_OPTIONS));
  if (cmd.aliases?.length) lines.push("", `Alias: tourclaim ${cmd.aliases.map((a) => a.join(" ")).join(", ")}`);
  if (cmd.examples?.length) lines.push("", "Examples:", ...cmd.examples.map((e) => `  ${e}`));
  lines.push("");
  return lines.join("\n");
}

interface Route {
  command: Command | null;
  group: string | null;
  rest: string[];
  helpTarget: string[] | null;
}

/** Finds the command words, skipping options anywhere before them. */
function route(argv: string[]): Route {
  const words: Array<{ word: string; index: number }> = [];
  for (let i = 0; i < argv.length && words.length < 2; i++) {
    const token = argv[i] ?? "";
    if (token === "--") break;
    if (token.startsWith("-") && token !== "-") {
      if (token === "--api-url") i++;
      continue;
    }
    words.push({ word: token, index: i });
  }
  const without = (count: number) => argv.filter((_, i) => !words.slice(0, count).some((w) => w.index === i));
  const first = words[0]?.word;
  if (first === undefined) return { command: null, group: null, rest: argv, helpTarget: null };
  if (first === "help") {
    const rest = without(1);
    return { command: null, group: null, rest, helpTarget: rest.filter((t) => !t.startsWith("-")) };
  }
  const single = COMMANDS.find(
    (c) => (c.path.length === 1 && c.path[0] === first) || c.aliases?.some((a) => a.length === 1 && a[0] === first),
  );
  if (single) return { command: single, group: null, rest: without(1), helpTarget: null };
  if (first in GROUPS) {
    const second = words[1]?.word;
    if (second === undefined) return { command: null, group: first, rest: without(1), helpTarget: null };
    const command = COMMANDS.find((c) => c.path[0] === first && c.path[1] === second);
    if (!command) throw new UsageError(`Unknown command "${first} ${second}". Run: tourclaim ${first} --help`);
    return { command, group: first, rest: without(2), helpTarget: null };
  }
  throw new UsageError(`Unknown command "${first}". Run: tourclaim --help`);
}

function helpFor(target: string[]): string {
  if (target.length === 0) return topHelp();
  const [a, b] = target;
  const cmd = COMMANDS.find(
    (c) => c.path.join(" ") === target.slice(0, c.path.length).join(" ") || c.aliases?.some((al) => al.join(" ") === a),
  );
  if (cmd && (cmd.path.length === 1 || b !== undefined)) return commandHelp(cmd);
  if (a && a in GROUPS) return groupHelp(a);
  throw new UsageError(`No help for "${target.join(" ")}". Run: tourclaim --help`);
}

function parse(cmd: Command, rest: string[]): Parsed {
  const options: Record<string, { type: "string" | "boolean"; short?: string; multiple?: boolean }> = {};
  for (const [name, spec] of Object.entries({ ...GLOBAL_OPTIONS, ...cmd.options })) {
    options[name] = { type: spec.type, ...(spec.short ? { short: spec.short } : {}), ...(spec.multiple ? { multiple: true } : {}) };
  }
  try {
    const { values, positionals } = parseArgs({ args: rest, options, allowPositionals: true, strict: true });
    return { values: values as Parsed["values"], positionals };
  } catch (error) {
    throw new UsageError(`${(error as Error).message.replace(/\.?$/, ".")} Run: tourclaim ${cmd.path.join(" ")} --help`);
  }
}

/** Runs the CLI and returns the exit code. Never calls process.exit. */
export async function run(argv: string[], deps: Deps): Promise<number> {
  const endOfOptions = argv.indexOf("--");
  const head = endOfOptions === -1 ? argv : argv.slice(0, endOfOptions);
  const out = new Output(deps.stdout, deps.stderr, head.includes("--json"));
  try {
    const r = route(argv);
    if (r.helpTarget) {
      out.raw(helpFor(r.helpTarget));
      return ExitCode.OK;
    }
    if (!r.command) {
      if (head.includes("--version") && !r.group) {
        if (out.json) out.data({ name: "tourclaim", version: VERSION });
        else out.raw(`${VERSION}\n`);
        return ExitCode.OK;
      }
      out.raw(r.group ? groupHelp(r.group) : topHelp());
      return ExitCode.OK;
    }
    const cmd = r.command;
    const args = parse(cmd, r.rest);
    if (args.values.version) {
      if (out.json) out.data({ name: "tourclaim", version: VERSION });
      else out.raw(`${VERSION}\n`);
      return ExitCode.OK;
    }
    if (args.values.help) {
      out.raw(commandHelp(cmd));
      return ExitCode.OK;
    }
    const n = args.positionals.length;
    if ((cmd.minArgs !== undefined && n < cmd.minArgs) || (cmd.maxArgs !== undefined && n > cmd.maxArgs)) {
      throw new UsageError(`${n < (cmd.minArgs ?? 0) ? "Missing arguments" : "Too many arguments"}. Usage: ${cmd.usage}`);
    }
    const apiUrl = resolveApiUrl(typeof args.values["api-url"] === "string" ? args.values["api-url"] : undefined, deps.env);
    const ctx = new Context(deps, out, apiUrl);
    const code = await cmd.run(ctx, args);
    return code ?? ExitCode.OK;
  } catch (error) {
    return out.error(error);
  }
}
