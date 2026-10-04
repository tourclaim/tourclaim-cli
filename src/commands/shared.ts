import { API_PREFIX, ApiError, sentences, type ApiClient } from "../api.js";
import type { Context } from "../context.js";
import { CliError, ExitCode, UsageError } from "../errors.js";
import type { IntakeResponse } from "../types.js";

/**
 * Asks for a yes/no confirmation on the terminal, or accepts --yes.
 * Without a terminal and without --yes it fails: there are no silent defaults.
 */
export async function confirm(ctx: Context, question: string, yes: boolean, howToSkip: string): Promise<void> {
  if (yes) return;
  if (!ctx.deps.stdinIsTTY) {
    throw new UsageError(`Confirmation needed and there is no terminal to ask on. ${howToSkip}`, { reason: "confirmation_required" });
  }
  const answer = (await ctx.deps.prompt(`${question} [y/N] `)).trim().toLowerCase();
  if (answer !== "y" && answer !== "yes") {
    throw new CliError("Cancelled; nothing was changed.", ExitCode.ERROR, "cancelled");
  }
}

export function draftNotFound(id: string, error: ApiError): ApiError {
  return new ApiError(
    404,
    `No draft ${id} that this key can reach. Drafts started with tourclaim login on this account are reachable from any later tourclaim login; drafts started by other apps (such as Muse) or with a key saved by login --with-token are reachable only with the key that started them. See the drafts this key can reach with: tourclaim intake list`,
    ExitCode.NOT_FOUND,
    "not_found",
    error.body,
  );
}

/** Rewrites a 404 into the draft-specific explanation. */
export async function intakeCall<T>(id: string, call: () => Promise<T>): Promise<T> {
  try {
    return await call();
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) throw draftNotFound(id, error);
    throw error;
  }
}

export function intakePath(id: string, suffix = ""): string {
  return `${API_PREFIX}/intakes/${encodeURIComponent(id)}${suffix}`;
}

export async function getIntake(api: ApiClient, id: string): Promise<IntakeResponse> {
  return intakeCall(id, async () => (await api.request<IntakeResponse>(intakePath(id))).data);
}

/** A conflict with the API's stable cause as its code. */
export function conflict(message: string, code: string, extra: Record<string, unknown> = {}): CliError {
  return new CliError(message, ExitCode.CONFLICT, code, { status: 409, ...extra });
}

/**
 * After a 409 on a write to a draft, re-reads the draft and explains what
 * happened, keyed on the API's X-TourClaim-Error code. Never retries.
 */
export async function conflictError(api: ApiClient, id: string, error: ApiError, triedRevision: number | undefined): Promise<CliError> {
  let current: IntakeResponse | null = null;
  try {
    current = await getIntake(api, id);
  } catch {
    // Keep the API's own explanation if the draft cannot be read.
  }
  const where = current ? { current_revision: current.revision, state: current.state } : {};
  const reason = typeof error.detail === "string" ? error.detail : "The draft could not be changed";
  if (error.code === "intake_submitted" || current?.state === "submitted") {
    return conflict(`Draft ${id} has been submitted and is read-only. Nothing was saved.`, "intake_submitted", where);
  }
  if (error.code === "evidence_conflict") {
    return conflict(
      `This draft already has that ${/email/i.test(reason) ? "message" : "evidence"} with different details (the same message id, or the same file bytes under another name or type). Nothing was saved.`,
      error.code,
      where,
    );
  }
  let advice: string;
  if (current && triedRevision !== undefined && current.revision !== triedRevision) {
    advice = `It is now at revision ${current.revision} (state ${current.state}); this command used revision ${triedRevision}. Nothing was saved. Check the current answers with: tourclaim intake show ${id}, then run the command again.`;
  } else if (current) {
    advice = `It is at revision ${current.revision} (state ${current.state}). Nothing was saved. Check it with: tourclaim intake show ${id}`;
  } else {
    advice = `Nothing was saved. Check the draft with: tourclaim intake show ${id}`;
  }
  return conflict(sentences(reason, advice), error.code, where);
}

/** The revision to send: --revision if given, else the draft's current one. */
export async function resolveRevision(api: ApiClient, id: string, given: number | undefined): Promise<{ revision: number; before: IntakeResponse | null }> {
  if (given !== undefined) return { revision: given, before: null };
  const before = await getIntake(api, id);
  return { revision: before.revision, before };
}

export function signatureVoidedNotice(ctx: Context, before: IntakeResponse | null, after: IntakeResponse): void {
  if (before?.state === "ready_to_submit" && after.state !== "ready_to_submit" && after.state !== "submitted") {
    ctx.out.info(
      `This change cancelled the traveler's signature. They must review and sign again: tourclaim intake sign ${after.id}`,
    );
  }
}
