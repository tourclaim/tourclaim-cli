import { API_PREFIX, ApiError } from "../api.js";
import { intArg, type Command } from "../command.js";
import { ExitCode } from "../errors.js";
import { claimLines, formatTime, modeNotice, table } from "../format.js";
import type { ClaimResponse } from "../types.js";

const PAGE_SIZE = 30;

export const claimsList: Command = {
  path: ["claims", "list"],
  summary: "List the traveler's submitted claims",
  usage: "tourclaim claims list [--offset <n>]",
  description:
    "Lists claims submitted through the connector, newest first, 30 at a time. Drafts that were never submitted are not listed.",
  options: {
    offset: { type: "string", value: "<n>", description: "How many claims to skip (default 0)." },
  },
  maxArgs: 0,
  async run(ctx, args) {
    const offset = intArg(args, "offset") ?? 0;
    const api = await ctx.authed();
    const { data } = await api.request<ClaimResponse[]>(`${API_PREFIX}/claims`, { query: { offset } });
    if (ctx.json) {
      ctx.out.data(data);
      return ExitCode.OK;
    }
    if (!data.length) {
      ctx.out.line(offset ? "No more claims." : "No submitted claims yet.");
      return ExitCode.OK;
    }
    const notice = modeNotice(data[0]?.mode);
    if (notice && data[0]?.mode === "review") ctx.out.lines([notice, ""]);
    ctx.out.lines(table(["CLAIM", "STATUS", "UPDATED", "NEXT"], data.map((c) => [c.id, c.status, formatTime(c.updated_at), c.next_action])));
    if (data.length >= PAGE_SIZE) ctx.out.lines(["", `More claims may exist: tourclaim claims list --offset ${offset + data.length}`]);
    return ExitCode.OK;
  },
};

export const claimsShow: Command = {
  path: ["claims", "show"],
  summary: "Show where one claim stands and what happens next",
  usage: "tourclaim claims show <claim-id>",
  description: "Relay next_action to the traveler as written. A status is not a coverage decision unless it says so.",
  minArgs: 1,
  maxArgs: 1,
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    const api = await ctx.authed();
    let claim: ClaimResponse;
    try {
      claim = (await api.request<ClaimResponse>(`${API_PREFIX}/claims/${encodeURIComponent(id)}`)).data;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApiError(404, `No claim ${id} for this traveler. List claims with: tourclaim claims list`, ExitCode.NOT_FOUND, "not_found", error.body);
      }
      throw error;
    }
    if (ctx.json) ctx.out.data(claim);
    else ctx.out.lines(claimLines(claim, ctx.deps.now()));
    return ExitCode.OK;
  },
};
