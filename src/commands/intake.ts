import { createHash, randomUUID } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { basename } from "node:path";
import { API_PREFIX, ApiError, sentences, type ApiClient } from "../api.js";
import { flag, intArg, str, strs, type Command } from "../command.js";
import { absoluteUrl } from "../config.js";
import type { Context } from "../context.js";
import { parseEml } from "../eml.js";
import { CliError, ExitCode, UsageError } from "../errors.js";
import { buildFields } from "../fields.js";
import { detectContentType, formatBytes, MAX_ATTACHMENT_BYTES } from "../filetype.js";
import { claimLines, intakeLines, nextStepLines, table } from "../format.js";
import {
  DOC_TYPES,
  EMAIL_PROVIDERS,
  type ClaimResponse,
  type EmailProvider,
  type IntakeResponse,
} from "../types.js";
import {
  confirm,
  conflict,
  conflictError,
  draftNotFound,
  getIntake,
  intakeCall,
  intakePath,
  resolveRevision,
  signatureVoidedNotice,
} from "./shared.js";

const IDEMPOTENCY_KEY = /^[A-Za-z0-9_-]{8,100}$/;
const FILENAME = /^[^/\\\x00-\x1f]+$/;
const MAX_EMAIL_TEXT = 30_000;
const SIGN_POLL_MS = 5_000;
const DEFAULT_SIGN_TIMEOUT_S = 900;
const PAGE_SIZE = 30;

const REVISION_OPTION = {
  type: "string" as const,
  value: "<n>",
  description: "The revision you last saw. Default: read the draft's current revision first.",
};

function printIntake(ctx: Context, intake: IntakeResponse, title?: string): void {
  if (ctx.json) ctx.out.data(intake);
  else ctx.out.lines(intakeLines(intake, title));
}

/** Runs a write against a draft, explaining 404s and 409s. */
async function writeCall<T>(ctx: Context, id: string, revision: number, call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw draftNotFound(id, error);
    if (error instanceof ApiError && error.status === 409) throw await conflictError(await ctx.authed(), id, error, revision);
    throw error;
  }
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
}

async function readTextSource(ctx: Context, path: string): Promise<string> {
  try {
    const text = path === "-" ? await ctx.deps.readStdin() : await readFile(path, "utf8");
    return text.replace(/^﻿/, "");
  } catch (error) {
    throw new CliError(`Could not read ${path}: ${(error as Error).message}`);
  }
}

async function readBytes(ctx: Context, path: string): Promise<Buffer> {
  try {
    return path === "-" ? Buffer.from(await ctx.deps.readStdin(), "utf8") : await readFile(path);
  } catch (error) {
    throw new CliError(`Could not read ${path}: ${(error as Error).message}`);
  }
}

export const intakeStart: Command = {
  path: ["intake", "start"],
  summary: "Start a claim draft",
  usage: "tourclaim intake start [--set <field>=<value>]... [--fields-file <file>] [--idempotency-key <key>]",
  description: [
    "Starts a draft with whatever the traveler has already said; every field is optional here.",
    "The result lists what is still missing and up to three questions to ask next.",
    "A random Idempotency-Key is generated unless you pass one. If the command fails or times out,",
    "rerun it with the same --idempotency-key and the same fields to get the same draft instead of a second one.",
    "Check tourclaim intake list first: the traveler may already have a draft for this trip.",
    "",
    "Fields: merchant_name, booking_ref, trip_date (YYYY-MM-DD), booking_amount, refunded_amount (decimal, USD),",
    "currency (USD), card_product_id (from cards search), reason_category, narrative, cancellation_policy,",
    "other_insurance (YES, NO, UNSURE), other_insurance_details, medical.<answer>. See tourclaim intake set --help.",
  ].join("\n"),
  options: {
    set: { type: "string", multiple: true, value: "<field>=<value>", description: "A field to save. Repeatable. Use field:=<json> for a raw JSON value." },
    "fields-file": { type: "string", value: "<file>", description: "A JSON object of fields (- reads stdin). --set values override it." },
    "idempotency-key": { type: "string", value: "<key>", description: "8 to 100 letters, digits, - or _. Default: a random UUID." },
  },
  maxArgs: 0,
  examples: [
    "tourclaim intake start --set merchant_name=\"Example Air\" --set booking_ref=EXA-482913 --set reason_category=weather",
    "tourclaim intake start --fields-file answers.json --json",
  ],
  async run(ctx, args) {
    const fields = await buildFields({ file: str(args, "fields-file"), pairs: strs(args, "set"), readStdin: ctx.deps.readStdin });
    const key = str(args, "idempotency-key") ?? randomUUID();
    if (!IDEMPOTENCY_KEY.test(key)) throw new UsageError("--idempotency-key must be 8 to 100 letters, digits, - or _.");
    const api = await ctx.authed();
    let intake: IntakeResponse;
    try {
      intake = (
        await api.request<IntakeResponse>(`${API_PREFIX}/intakes`, {
          method: "POST",
          body: { fields },
          headers: { "Idempotency-Key": key },
        })
      ).data;
    } catch (error) {
      if (error instanceof CliError) {
        error.extra.idempotency_key = key;
        const transient = error.code === "concurrent_request";
        if (error.code === "idempotency_key_reused" || error.code === "idempotency_key_other_connection") {
          error.message = sentences(error.message, "Use a new --idempotency-key (or leave it out) to start a different draft.");
        } else if (transient || !(error instanceof ApiError) || error.status >= 429) {
          error.message = `${error.message} The draft may or may not have been created. Retry with the same fields and --idempotency-key ${key} so a duplicate is not created.`;
        }
      }
      throw error;
    }
    if (ctx.json) {
      ctx.out.data(intake);
      return ExitCode.OK;
    }
    ctx.out.lines(intakeLines(intake, "Started draft"));
    ctx.out.lines(["", "Find it again any time with: tourclaim intake list"]);
    return ExitCode.OK;
  },
};

export const intakeList: Command = {
  path: ["intake", "list"],
  summary: "List drafts not yet submitted",
  usage: "tourclaim intake list [--offset <n>]",
  description: [
    "Lists the traveler's drafts that were never submitted, most recently changed first, 30 at a time.",
    "A key from tourclaim login reaches the drafts started from any tourclaim login on the same account, so",
    "signing in again or a key running out does not lose a draft. Drafts started by other apps (such as Muse),",
    "or with a key saved by login --with-token, are listed only with the key that started them.",
    "Submitted drafts are claims: see tourclaim claims list.",
  ].join("\n"),
  options: {
    offset: { type: "string", value: "<n>", description: "How many drafts to skip (default 0)." },
  },
  maxArgs: 0,
  async run(ctx, args) {
    const offset = intArg(args, "offset") ?? 0;
    const api = await ctx.authed();
    const { data } = await api.request<IntakeResponse[]>(`${API_PREFIX}/intakes`, { query: { offset } });
    if (ctx.json) {
      ctx.out.data(data);
      return ExitCode.OK;
    }
    if (!data.length) {
      ctx.out.line(offset ? "No more drafts." : "No open drafts. Start one with: tourclaim intake start");
      return ExitCode.OK;
    }
    ctx.out.lines(
      table(
        ["DRAFT", "STATE", "MERCHANT", "BOOKING", "MISSING"],
        data.map((d) => [
          d.id,
          d.state,
          d.fields?.merchant_name ?? "-",
          d.fields?.booking_ref ?? "-",
          d.missing_fields.length ? String(d.missing_fields.length) : "-",
        ]),
      ),
    );
    if (data.length >= PAGE_SIZE) ctx.out.lines(["", `More drafts may exist: tourclaim intake list --offset ${offset + data.length}`]);
    ctx.out.lines(["", "Show one with: tourclaim intake show <draft>"]);
    return ExitCode.OK;
  },
};

export const intakeShow: Command = {
  path: ["intake", "show"],
  summary: "Show a draft, what it still needs and its state",
  usage: "tourclaim intake show <id>",
  minArgs: 1,
  maxArgs: 1,
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    const intake = await getIntake(await ctx.authed(), id);
    printIntake(ctx, intake);
    return ExitCode.OK;
  },
};

export const intakeSet: Command = {
  path: ["intake", "set"],
  summary: "Save answers to a draft",
  usage: "tourclaim intake set <id> [<field>=<value>]... [<field>:=<json>]... [--clear <field>]... [--fields-file <file>]",
  description: [
    "Saves only the fields given; everything else is unchanged. --clear sets a field to null (clears it).",
    "field=value is typed by the schema: amounts like 250.00, dates like 2026-03-04, true/false answers, and",
    "enum values in any case (weather becomes WEATHER). field:=<json> sends a JSON value as is.",
    "Medical answers are addressed as medical.<answer>, for example medical.provider_seen=true.",
    "",
    "The command reads the current revision and sends it as expected_revision. If the draft changed in between,",
    "nothing is saved: the command exits 4 and says what the draft looks like now. It does not retry on its own.",
    "Any change after the traveler signed cancels the signature; they must sign again.",
    "",
    "Fields: merchant_name, booking_ref, trip_date, booking_amount, refunded_amount, currency, card_product_id,",
    "reason_category (ILLNESS, INJURY, DEATH_IN_FAMILY, MILITARY_DEPLOYMENT, WEATHER, AIRLINE_CANCELLATION, OTHER),",
    "narrative, cancellation_policy, other_insurance (YES, NO, UNSURE), other_insurance_details,",
    "medical.symptom_onset_date, medical.provider_seen, medical.provider_details, medical.prior_condition,",
    "medical.prior_condition_details, medical.stability_60day, medical.stability_60day_details.",
  ].join("\n"),
  options: {
    clear: { type: "string", multiple: true, value: "<field>", description: "Clear a field (send null). Repeatable." },
    "fields-file": { type: "string", value: "<file>", description: "A JSON object of fields (- reads stdin). Pairs override it." },
    revision: REVISION_OPTION,
  },
  minArgs: 1,
  examples: [
    "tourclaim intake set 1148bfae-... trip_date=2026-03-04 booking_amount=250.00 refunded_amount=50.00 currency=USD",
    "tourclaim intake set 1148bfae-... card_product_id=412 other_insurance=no",
    "tourclaim intake set 1148bfae-... narrative=\"The harbor closed for a storm warning and the tour was cancelled.\"",
    "tourclaim intake set 1148bfae-... medical.provider_seen=true --clear medical.provider_details",
  ],
  async run(ctx, args) {
    const [id = "", ...pairs] = args.positionals;
    const fields = await buildFields({
      file: str(args, "fields-file"),
      pairs,
      clears: strs(args, "clear"),
      readStdin: ctx.deps.readStdin,
    });
    if (Object.keys(fields).length === 0) {
      throw new UsageError("Nothing to change. Give <field>=<value> pairs, --clear <field> or --fields-file.");
    }
    const api = await ctx.authed();
    const { revision, before } = await resolveRevision(api, id, intArg(args, "revision", 1));
    if (before?.state === "submitted") {
      throw conflict(`Draft ${id} has been submitted and is read-only. Nothing was saved.`, "intake_submitted", { state: "submitted" });
    }
    const intake = await writeCall(ctx, id, revision, async () =>
      (await api.request<IntakeResponse>(intakePath(id), { method: "PATCH", body: { expected_revision: revision, fields } })).data,
    );
    printIntake(ctx, intake, "Saved draft");
    signatureVoidedNotice(ctx, before, intake);
    return ExitCode.OK;
  },
};

export const intakeDelete: Command = {
  path: ["intake", "delete"],
  summary: "Delete a draft that was never submitted",
  usage: "tourclaim intake delete <id> [--yes]",
  description:
    "Permanently deletes the draft with every answer, email and file saved against it. Asks for confirmation unless --yes is given.\nA submitted draft is a claim and cannot be deleted here.",
  options: {
    yes: { type: "boolean", short: "y", description: "Skip the confirmation (only after the traveler confirmed)." },
  },
  minArgs: 1,
  maxArgs: 1,
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    await confirm(
      ctx,
      `Delete draft ${id} and every answer, email and file saved with it? This cannot be undone.`,
      flag(args, "yes"),
      "Pass --yes once the traveler has confirmed they want this draft deleted.",
    );
    const api = await ctx.authed();
    try {
      await api.request(intakePath(id), { method: "DELETE" });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) throw draftNotFound(id, error);
      if (error instanceof ApiError && error.status === 409 && error.code === "intake_submitted") {
        throw conflict(
          `Draft ${id} was submitted as a claim and cannot be deleted here. To request deletion, see ${absoluteUrl(ctx.apiUrl, "/muse/data-deletion")}`,
          error.code,
        );
      }
      if (error instanceof ApiError && error.status === 409) throw await conflictError(api, id, error, undefined);
      throw error;
    }
    if (ctx.json) ctx.out.data({ id, deleted: true });
    else ctx.out.line(`Deleted draft ${id}.`);
    return ExitCode.OK;
  },
};

export const intakeAttach: Command = {
  path: ["intake", "attach"],
  summary: "Attach a PDF, JPEG or PNG the traveler chose to share",
  usage: "tourclaim intake attach <id> <file> --type <receipt|itinerary|medical_note|other> [--yes]",
  description: [
    "Uploads one file as evidence: a receipt, itinerary, or a doctor's note the traveler already has.",
    "The type is detected from the file's contents (PDF, JPEG or PNG only); the limit is 5 MiB.",
    "Sharing needs the traveler's agreement: the command asks on the terminal, or takes --yes.",
    "Pass --yes only after the traveler agreed to share this exact file. Without a terminal and without",
    "--yes it refuses and uploads nothing.",
    "A medical_note here is evidence the traveler supplied; it is not a note issued by a Copernican provider.",
  ].join("\n"),
  options: {
    type: { type: "string", value: "<type>", description: `What the file is: ${DOC_TYPES.join(", ")}. Required.` },
    yes: { type: "boolean", short: "y", description: "The traveler agreed to share this file." },
    revision: REVISION_OPTION,
  },
  minArgs: 2,
  maxArgs: 2,
  examples: ["tourclaim intake attach 1148bfae-... hotel-receipt.pdf --type receipt"],
  async run(ctx, args) {
    const [id = "", path = ""] = args.positionals;
    const docType = str(args, "type");
    if (!docType) throw new UsageError(`--type is required: ${DOC_TYPES.join(", ")}.`);
    if (!(DOC_TYPES as readonly string[]).includes(docType)) throw new UsageError(`--type must be one of: ${DOC_TYPES.join(", ")}.`);

    let size: number;
    try {
      const info = await stat(path);
      if (!info.isFile()) throw new CliError(`${path} is not a file.`);
      size = info.size;
    } catch (error) {
      if (error instanceof CliError) throw error;
      throw new CliError(`Could not read ${path}: ${(error as NodeJS.ErrnoException).code === "ENOENT" ? "no such file" : (error as Error).message}.`);
    }
    if (size === 0) throw new CliError(`${path} is empty.`);
    if (size > MAX_ATTACHMENT_BYTES) {
      throw new CliError(`${path} is ${formatBytes(size)}; the limit is 5 MiB. Ask the traveler for a smaller copy.`, ExitCode.ERROR, "too_large");
    }
    const bytes = await readBytes(ctx, path);
    const contentType = detectContentType(bytes);
    if (!contentType) {
      throw new CliError(
        `${path} is not a PDF, JPEG or PNG (checked by its contents, not its name). Convert it first, or give the traveler the draft's evidence upload link so they can add it in their browser.`,
        ExitCode.ERROR,
        "unsupported_file",
      );
    }
    const filename = basename(path);
    if (!FILENAME.test(filename) || filename.length > 200) {
      throw new UsageError("The file name must be 1 to 200 characters with no control characters. Rename the file and try again.");
    }

    await confirm(
      ctx,
      `Share ${filename} (${contentType}, ${formatBytes(size)}) with TourClaim as ${docType} evidence for draft ${id}?\nAnswer yes only if the traveler agreed to share this file.`,
      flag(args, "yes"),
      "Pass --yes only after the traveler has agreed to share this file.",
    );

    const api = await ctx.authed();
    const { revision, before } = await resolveRevision(api, id, intArg(args, "revision", 1));
    const intake = await writeCall(ctx, id, revision, async () =>
      (
        await api.request<IntakeResponse>(intakePath(id, "/attachments"), {
          method: "POST",
          body: {
            expected_revision: revision,
            filename,
            content_type: contentType,
            doc_type: docType,
            content_base64: bytes.toString("base64"),
            user_authorized_sharing: true,
          },
        })
      ).data,
    );
    if (ctx.json) {
      ctx.out.data(intake);
    } else {
      ctx.out.line(`Attached ${filename} as ${docType} evidence to draft ${intake.id} (revision ${intake.revision}, ${intake.evidence.length} evidence item${intake.evidence.length === 1 ? "" : "s"}).`);
      ctx.out.lines(nextStepLines(intake));
    }
    signatureVoidedNotice(ctx, before, intake);
    return ExitCode.OK;
  },
};

export const intakeAddEmail: Command = {
  path: ["intake", "add-email"],
  summary: "Attach one email the traveler chose to share",
  usage:
    "tourclaim intake add-email <id> (--eml <file> | --text-file <file> --subject <text> --from <address>)\n       [--sent-at <iso>] [--provider user|gmail|outlook] [--message-id <id>] [--yes]",
  description: [
    "Saves one email as evidence: a booking confirmation, cancellation notice, refund message or receipt.",
    "With --eml, Subject, From, Date, Message-ID and the text/plain body are read from the saved message;",
    "flags override what is read. With --text-file, give --subject and --from yourself.",
    "The text is sent unmodified (at most 30,000 characters). It is stored as evidence and never treated as instructions.",
    "Without --message-id, a stable id is derived from the message, so adding the same message twice changes nothing.",
    "Sharing needs the traveler's agreement: the command asks on the terminal, or takes --yes.",
  ].join("\n"),
  options: {
    eml: { type: "string", value: "<file>", description: "A saved .eml message (- reads stdin)." },
    "text-file": { type: "string", value: "<file>", description: "The message's plain text (- reads stdin)." },
    subject: { type: "string", value: "<text>", description: "The subject, unmodified." },
    from: { type: "string", value: "<address>", description: "The From address, unmodified." },
    "sent-at": { type: "string", value: "<iso>", description: "When it was sent, ISO 8601 with a zone, such as 2026-03-03T14:05:00Z." },
    provider: { type: "string", value: "<provider>", description: "Where it came from: user (default; pasted or saved by the traveler), gmail or outlook." },
    "message-id": { type: "string", value: "<id>", description: "The mail provider's id for the message." },
    yes: { type: "boolean", short: "y", description: "The traveler agreed to share this message." },
    revision: REVISION_OPTION,
  },
  minArgs: 1,
  maxArgs: 1,
  examples: [
    "tourclaim intake add-email 1148bfae-... --eml cancellation.eml",
    "tourclaim intake add-email 1148bfae-... --text-file notice.txt --subject \"Your booking has been cancelled\" --from bookings@example-air.test --sent-at 2026-03-03T14:05:00Z --yes",
  ],
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    const emlPath = str(args, "eml");
    const textPath = str(args, "text-file");
    if (Boolean(emlPath) === Boolean(textPath)) throw new UsageError("Give exactly one of --eml <file> or --text-file <file>.");

    let subject = str(args, "subject");
    let from = str(args, "from");
    let sentAt = str(args, "sent-at");
    let messageId = str(args, "message-id");
    let text: string;
    if (emlPath) {
      const parsed = parseEml(await readBytes(ctx, emlPath));
      if (parsed.text === null) {
        throw new CliError(`${emlPath} has no text/plain part. Save the message as plain text and use --text-file.`, ExitCode.ERROR, "unsupported_email");
      }
      text = parsed.text;
      subject ??= parsed.subject ?? "";
      from ??= parsed.from ?? undefined;
      sentAt ??= parsed.date ?? undefined;
      messageId ??= parsed.messageId ?? undefined;
    } else {
      text = await readTextSource(ctx, textPath ?? "");
      if (subject === undefined) throw new UsageError("--subject is required with --text-file.");
      if (!from) throw new UsageError("--from is required with --text-file.");
    }
    if (!from) throw new UsageError("The message has no From header; pass --from.");
    const provider = (str(args, "provider") ?? "user") as EmailProvider;
    if (!EMAIL_PROVIDERS.includes(provider)) throw new UsageError(`--provider must be one of: ${EMAIL_PROVIDERS.join(", ")}.`);
    if (sentAt !== undefined) {
      if (!/T.*(Z|[+-]\d{2}:?\d{2})$/i.test(sentAt) || Number.isNaN(Date.parse(sentAt))) {
        throw new UsageError("--sent-at must be an ISO 8601 date and time with a zone, such as 2026-03-03T14:05:00Z.");
      }
      sentAt = new Date(Date.parse(sentAt)).toISOString();
    }
    if (!text.trim()) throw new CliError("The message text is empty.");
    const length = [...text].length;
    if (length > MAX_EMAIL_TEXT) {
      throw new CliError(
        `The message text is ${length.toLocaleString("en-US")} characters; the API accepts at most 30,000. Share the one relevant message, unmodified; do not summarize it.`,
      );
    }
    if (subject.length > 500) throw new UsageError("The subject is longer than 500 characters.");
    if (from.length > 320) throw new UsageError("The From address is longer than 320 characters.");
    messageId ??= `sha256-${createHash("sha256").update(`${from}\n${subject}\n${sentAt ?? ""}\n${text}`).digest("hex").slice(0, 40)}`;
    if (!messageId || messageId.length > 255) throw new UsageError("--message-id must be 1 to 255 characters.");

    await confirm(
      ctx,
      [
        `Share this email with TourClaim as evidence for draft ${id}?`,
        `  From:    ${from}`,
        `  Subject: ${subject}`,
        `  Sent:    ${sentAt ?? "not given"}`,
        `  Text:    ${length} characters`,
        "Answer yes only if the traveler agreed to share this message.",
      ].join("\n"),
      flag(args, "yes"),
      "Pass --yes only after the traveler has seen which message will be shared and agreed.",
    );

    const api = await ctx.authed();
    const { revision, before } = await resolveRevision(api, id, intArg(args, "revision", 1));
    const intake = await writeCall(ctx, id, revision, async () =>
      (
        await api.request<IntakeResponse>(intakePath(id, "/email-evidence"), {
          method: "POST",
          body: {
            expected_revision: revision,
            provider,
            message_id: messageId,
            subject,
            sender: from,
            sent_at: sentAt ?? null,
            text,
            user_authorized_sharing: true,
          },
        })
      ).data,
    );
    if (ctx.json) {
      ctx.out.data(intake);
    } else {
      ctx.out.line(`Added the email to draft ${intake.id} (revision ${intake.revision}, ${intake.evidence.length} evidence item${intake.evidence.length === 1 ? "" : "s"}).`);
      ctx.out.lines(nextStepLines(intake));
    }
    signatureVoidedNotice(ctx, before, intake);
    return ExitCode.OK;
  },
};

export const intakeSign: Command = {
  path: ["intake", "sign"],
  summary: "Send the traveler to review and sign; optionally wait until they have",
  usage: "tourclaim intake sign <id> [--wait] [--timeout <seconds>] [--no-browser]",
  description: [
    "Only the traveler can sign. They review the draft and sign the authorization in their own browser at the",
    "review link; this tool cannot sign for them and does not automate that page.",
    "When the draft needs approval, this prints the link and opens it when run in a terminal.",
    "--wait checks every 5 seconds until the traveler has signed (state ready_to_submit) or --timeout passes.",
    "With --json and --wait, the first line is a waiting_for_signature event carrying review_url; the last line is the draft.",
  ].join("\n"),
  options: {
    wait: { type: "boolean", description: "Wait until the traveler has signed." },
    timeout: { type: "string", value: "<seconds>", description: `How long --wait waits (default ${DEFAULT_SIGN_TIMEOUT_S}).` },
    "no-browser": { type: "boolean", description: "Do not open the review link in a browser." },
  },
  minArgs: 1,
  maxArgs: 1,
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    const timeout = intArg(args, "timeout", 1) ?? DEFAULT_SIGN_TIMEOUT_S;
    const wait = flag(args, "wait");
    const api = await ctx.authed();
    const intake = await getIntake(api, id);

    if (intake.state === "collecting") throw incomplete(id, intake);
    if (intake.state === "ready_to_submit" || intake.state === "submitted") {
      if (ctx.json) ctx.out.data(intake);
      else if (intake.state === "submitted") ctx.out.line(`Draft ${id} was already signed and submitted as claim ${intake.claim_id}.`);
      else ctx.out.lines([`The traveler has already signed revision ${intake.revision}.`, `Next: tourclaim intake submit ${id}`]);
      return ExitCode.OK;
    }

    const reviewUrl = intake.review_url;
    if (!isHttpUrl(reviewUrl)) throw new CliError("The API did not return a review link for this draft.", ExitCode.ERROR, "bad_response");
    if (!ctx.json) {
      ctx.out.lines([
        "Only the traveler can sign. They review this draft and sign the authorization in their own browser;",
        "this tool cannot sign for them.",
        "",
        `  Review and sign: ${reviewUrl}`,
        "",
      ]);
    }
    let opened = false;
    if (ctx.deps.stdoutIsTTY && !flag(args, "no-browser")) opened = await ctx.deps.openUrl(reviewUrl);
    if (opened && !ctx.json) ctx.out.line("Opened the link in your browser.");

    if (!wait) {
      if (ctx.json) ctx.out.data(intake);
      else ctx.out.line(`When they have signed, run: tourclaim intake submit ${id}   (or wait with: tourclaim intake sign ${id} --wait)`);
      return ExitCode.OK;
    }

    if (ctx.json) ctx.out.data({ event: "waiting_for_signature", intake_id: id, review_url: reviewUrl, timeout_seconds: timeout });
    const minutes = Math.round(timeout / 60);
    ctx.out.info(
      `Waiting for the traveler to sign (checking every 5 seconds for up to ${minutes >= 1 ? `${minutes} minute${minutes === 1 ? "" : "s"}` : `${timeout} seconds`}). Ctrl-C stops waiting; the link stays valid.`,
    );
    const deadline = ctx.deps.now() + timeout * 1000;
    while (ctx.deps.now() + SIGN_POLL_MS <= deadline) {
      await ctx.deps.sleep(SIGN_POLL_MS);
      const current = await getIntake(api, id);
      if (current.state === "ready_to_submit" || current.state === "submitted") {
        if (ctx.json) ctx.out.data(current);
        else ctx.out.lines([`Signed: the traveler approved revision ${current.revision}.`, ...nextStepLines(current)]);
        return ExitCode.OK;
      }
      if (current.state === "collecting") {
        throw conflict(`Draft ${id} changed while waiting and needs answers again: ${current.missing_fields.join(", ")}.`, "intake_incomplete", {
          state: current.state,
          current_revision: current.revision,
          missing_fields: current.missing_fields,
        });
      }
    }
    throw new CliError(
      `The traveler has not signed after ${timeout} seconds. The review link stays valid: ${reviewUrl}. Run this command again to keep waiting.`,
      ExitCode.ERROR,
      "timeout",
      { review_url: reviewUrl },
    );
  },
};

export const intakeSubmit: Command = {
  path: ["intake", "submit"],
  summary: "Submit a draft the traveler has signed",
  usage: "tourclaim intake submit <id> [--revision <n>]",
  description: [
    "Hands a complete, signed draft to Copernican as a claim. Safe to retry: a repeated call returns the same claim.",
    "This does not file anything with an insurer, charge a fee or promise reimbursement.",
    "Refused (exit 4) when the draft changed (stale_revision), the traveler has not signed the current revision",
    "(approval_required), the draft is incomplete (intake_incomplete), the signature is older than 7 days or the",
    "authorization changed (approval_outdated), or the booking already has a claim (duplicate_booking).",
  ].join("\n"),
  options: { revision: { ...REVISION_OPTION, description: "The revision the traveler signed. Default: the draft's current revision." } },
  minArgs: 1,
  maxArgs: 1,
  async run(ctx, args) {
    const id = args.positionals[0] ?? "";
    const api = await ctx.authed();
    let revision = intArg(args, "revision", 1);
    if (revision === undefined) {
      const intake = await getIntake(api, id);
      // Answers missing: say which. Anything else (unsigned, or a signature
      // that is out of date) the API names more precisely than the state does.
      if (intake.state === "collecting") throw incomplete(id, intake);
      revision = intake.revision;
    }
    let claim: ClaimResponse;
    try {
      claim = (
        await intakeCall(id, () => api.request<ClaimResponse>(intakePath(id, "/submit"), { method: "POST", body: { expected_revision: revision } }))
      ).data;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) throw await submitConflict(api, id, error);
      throw error;
    }
    if (ctx.json) {
      ctx.out.data(claim);
      return ExitCode.OK;
    }
    ctx.out.lines([`Submitted draft ${id} as claim ${claim.id}.`, "", ...claimLines(claim, ctx.deps.now()), ""]);
    ctx.out.lines([
      "Copernican reviews the claim next. Nothing here decides coverage or promises reimbursement.",
      `Check its status any time: tourclaim claims show ${claim.id}`,
    ]);
    return ExitCode.OK;
  },
};

function incomplete(id: string, intake: IntakeResponse): CliError {
  return conflict(
    `Draft ${id} is not complete; it still needs: ${intake.missing_fields.join(", ")}. Save those with tourclaim intake set first.`,
    "intake_incomplete",
    { state: intake.state, current_revision: intake.revision, missing_fields: intake.missing_fields },
  );
}

function notSigned(id: string, intake: IntakeResponse, code = "approval_required", reason?: string): CliError {
  const why =
    code === "approval_outdated"
      ? "The traveler's signature is out of date (it is older than 7 days, or the authorization changed); they must review and sign again."
      : `The traveler has not signed revision ${intake.revision} of draft ${id}.`;
  const link = intake.review_url ? `Only they can sign, in their own browser, at: ${intake.review_url}` : "Only they can sign, in their own browser.";
  return conflict(
    sentences(reason && code !== "approval_outdated" ? reason : undefined, why, link, `Get the link and wait for the signature with: tourclaim intake sign ${id} --wait`),
    code,
    { review_url: intake.review_url, current_revision: intake.revision, state: intake.state },
  );
}

/** Explains a refused submission by its X-TourClaim-Error code, after re-reading the draft. */
async function submitConflict(api: ApiClient, id: string, error: ApiError): Promise<CliError> {
  let current: IntakeResponse | null = null;
  try {
    current = await getIntake(api, id);
  } catch {
    // Fall back to the API's own explanation.
  }
  const reason = typeof error.detail === "string" ? error.detail : "The draft cannot be submitted";
  const where = current ? { state: current.state, current_revision: current.revision } : {};
  switch (error.code) {
    case "approval_required":
    case "approval_outdated":
      if (current) return notSigned(id, current, error.code, reason);
      break;
    case "intake_incomplete":
      if (current) return incomplete(id, current);
      break;
    case "duplicate_booking":
      return conflict(
        sentences(reason, "One claim per booking: this booking reference already has a claim for this traveler. See tourclaim claims list"),
        error.code,
        where,
      );
    case "stale_revision":
      return conflict(
        sentences(reason, `The draft changed${current ? ` (it is now at revision ${current.revision}, state ${current.state})` : ""}. Check it with: tourclaim intake show ${id}`),
        error.code,
        where,
      );
  }
  return conflict(sentences(reason, `Check it with: tourclaim intake show ${id}`), error.code, where);
}
