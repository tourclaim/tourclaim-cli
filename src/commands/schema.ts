import { API_PREFIX, ApiError } from "../api.js";
import type { Command } from "../command.js";
import { ExitCode } from "../errors.js";

/** Every operation, including the drafts list and the key endpoints the CLI uses. */
export const CLI_SCHEMA_PATH = `${API_PREFIX}/openapi-cli.json`;
/** The ten operations assistants load as tools; servers before 1.57.1 serve everything here. */
export const ASSISTANT_SCHEMA_PATH = `${API_PREFIX}/openapi.json`;

export const schema: Command = {
  path: ["schema"],
  summary: "Print the live OpenAPI schema",
  usage: "tourclaim schema [--json]",
  description: [
    `Fetches the full schema the CLI uses, ${CLI_SCHEMA_PATH}, and prints it: indented by default, one line`,
    `with --json. No sign-in needed. From a server that does not have it yet, it prints ${ASSISTANT_SCHEMA_PATH}.`,
    `${ASSISTANT_SCHEMA_PATH} itself lists only the ten operations assistants load as tools.`,
  ].join("\n"),
  maxArgs: 0,
  async run(ctx) {
    const api = ctx.client();
    let data: unknown;
    try {
      data = (await api.request<unknown>(CLI_SCHEMA_PATH, { auth: false })).data;
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) throw error;
      data = (await api.request<unknown>(ASSISTANT_SCHEMA_PATH, { auth: false })).data;
    }
    if (ctx.json) ctx.out.data(data);
    else ctx.out.raw(JSON.stringify(data, null, 2) + "\n");
    return ExitCode.OK;
  },
};
