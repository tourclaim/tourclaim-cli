import { API_PREFIX } from "../api.js";
import type { Command } from "../command.js";
import { ExitCode, UsageError } from "../errors.js";
import { cardLines } from "../format.js";
import type { CardResponse } from "../types.js";

export const cardsSearch: Command = {
  path: ["cards", "search"],
  summary: "Find the card a booking was paid with",
  usage: "tourclaim cards search <query...>",
  description: [
    "Searches the card catalog by product name, for example \"Sapphire\" or \"Venture\" (at most 30 matches).",
    "Use the id as card_product_id. A card appearing here does not mean it covers the loss.",
    "Never type or send a card number, expiry date or security code.",
  ].join("\n"),
  minArgs: 1,
  examples: ["tourclaim cards search sapphire preferred", "tourclaim cards search venture --json"],
  async run(ctx, args) {
    const q = args.positionals.join(" ").trim();
    if (!q) throw new UsageError("Give part of the card's product name, such as: tourclaim cards search sapphire");
    if (q.length > 100) throw new UsageError("The search text can be at most 100 characters.");
    if (/\d{12,}/.test(q.replace(/[\s-]/g, ""))) {
      throw new UsageError("That looks like a card number. Search by the card's product name instead; never share card numbers.");
    }
    const api = await ctx.authed();
    const { data } = await api.request<CardResponse[]>(`${API_PREFIX}/cards`, { query: { q } });
    if (ctx.json) {
      ctx.out.data(data);
      return ExitCode.OK;
    }
    if (!data.length) {
      ctx.out.line(`No cards matched "${q}". Try a shorter part of the name.`);
      return ExitCode.OK;
    }
    ctx.out.lines(cardLines(data));
    ctx.out.lines([
      "",
      "Finding a card here does not mean it covers the loss; Copernican reviews coverage.",
      `Use the id as card_product_id, for example: tourclaim intake set <draft-id> card_product_id=${data[0]?.id ?? "<id>"}`,
    ]);
    return ExitCode.OK;
  },
};
