import { API_PREFIX } from "../api.js";
import type { Command } from "../command.js";
import { ExitCode } from "../errors.js";

export const schema: Command = {
  path: ["schema"],
  summary: "Print the live OpenAPI schema",
  usage: "tourclaim schema [--json]",
  description: `Fetches ${API_PREFIX}/openapi.json (no sign-in needed) and prints it: indented by default, one line with --json.`,
  maxArgs: 0,
  async run(ctx) {
    const { data } = await ctx.client().request<unknown>(`${API_PREFIX}/openapi.json`, { auth: false });
    if (ctx.json) ctx.out.data(data);
    else ctx.out.raw(JSON.stringify(data, null, 2) + "\n");
    return ExitCode.OK;
  },
};
