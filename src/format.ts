import type { CardResponse, ClaimResponse, IntakeResponse, IntakeState, Mode } from "./types.js";

/**
 * The API sends some timestamps without a zone (for example key expiry,
 * "2026-11-03T18:00:00"). They are UTC.
 */
export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const ms = Date.parse(hasZone || !value.includes("T") ? value : `${value}Z`);
  return Number.isNaN(ms) ? null : new Date(ms);
}

export function formatTime(value: string | null | undefined): string {
  const date = parseServerTime(value);
  if (!date) return value ?? "unknown";
  return date.toISOString().replace("T", " ").replace(/:\d{2}(\.\d+)?Z$/, " UTC");
}

/** "in 3 days", "in 5 hours", "2 days ago". */
export function relativeTime(value: string | null | undefined, now: number): string {
  const date = parseServerTime(value);
  if (!date) return "";
  const diff = date.getTime() - now;
  const abs = Math.abs(diff);
  const hour = 3_600_000;
  const day = 24 * hour;
  if (abs < 60_000) return "just now";
  const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "s"}`;
  let amount: string;
  if (abs >= day) amount = plural(Math.round(abs / day), "day");
  else if (abs >= hour) amount = plural(Math.round(abs / hour), "hour");
  else amount = plural(Math.round(abs / 60_000), "minute");
  return diff >= 0 ? `in ${amount}` : `${amount} ago`;
}

export function modeNotice(mode: Mode | string | null | undefined): string | null {
  if (mode === "review") return "review mode: claims are synthetic and nothing is filed";
  if (mode === "live") return "live: submitted claims go to Copernican for review and filing";
  return null;
}

const STATE_TEXT: Record<IntakeState, string> = {
  collecting: "collecting (answers are missing)",
  needs_approval: "needs_approval (complete; the traveler must review and sign)",
  ready_to_submit: "ready_to_submit (signed; ready to submit)",
  submitted: "submitted (read-only; a claim exists)",
};

function valueText(value: unknown): string {
  if (typeof value === "string") return value.replace(/\n/g, "\n                          ");
  return JSON.stringify(value);
}

/** Human summary of an intake. */
export function intakeLines(intake: IntakeResponse, title = "Draft"): string[] {
  const lines: string[] = [`${title} ${intake.id}`];
  const pad = (label: string) => `  ${label.padEnd(10)} `;
  lines.push(pad("State:") + (STATE_TEXT[intake.state] ?? intake.state));
  lines.push(pad("Revision:") + String(intake.revision));
  const notice = modeNotice(intake.mode);
  if (notice) lines.push(pad("Mode:") + notice);
  lines.push(pad("Coverage:") + "not determined (nothing here decides whether the loss is covered)");
  if (intake.evidence.length) {
    lines.push(pad("Evidence:") + intake.evidence.map((e) => e.label).join(", "));
  } else {
    lines.push(pad("Evidence:") + "none yet");
  }

  const saved: Array<[string, unknown]> = [];
  for (const [key, value] of Object.entries(intake.fields ?? {})) {
    if (value === null || value === undefined) continue;
    if (key === "medical" && typeof value === "object") {
      for (const [mKey, mValue] of Object.entries(value as Record<string, unknown>)) {
        if (mValue !== null && mValue !== undefined) saved.push([`medical.${mKey}`, mValue]);
      }
    } else {
      saved.push([key, value]);
    }
  }
  if (saved.length) {
    lines.push("", "Saved answers:");
    for (const [key, value] of saved) lines.push(`  ${key.padEnd(24)}${valueText(value)}`);
  }
  if (intake.missing_fields.length) {
    lines.push("", `Missing (${intake.missing_fields.length}): ${intake.missing_fields.join(", ")}`);
  }
  if (intake.next_questions.length) {
    lines.push("", "Ask the traveler next:");
    intake.next_questions.forEach((q, i) => lines.push(`  ${i + 1}. ${q}`));
  }
  const reason = intake.fields?.reason_category;
  if ((reason === "ILLNESS" || reason === "INJURY") && intake.medical_note_pathway) {
    lines.push("", `Medical documentation: ${intake.medical_note_pathway}`);
  }
  lines.push("", ...nextStepLines(intake));
  return lines;
}

export function nextStepLines(intake: IntakeResponse): string[] {
  switch (intake.state) {
    case "collecting":
      return [`Next: tourclaim intake set ${intake.id} <field>=<value> ...`];
    case "needs_approval":
      return [
        "Next: the traveler reviews and signs in their own browser. This tool cannot sign for them.",
        `  Review link: ${intake.review_url ?? "(run tourclaim intake show to get it)"}`,
        `  Then: tourclaim intake sign ${intake.id} --wait`,
      ];
    case "ready_to_submit":
      return [`Next: tourclaim intake submit ${intake.id}`];
    case "submitted":
      return intake.claim_id ? [`Claim: ${intake.claim_id}  (tourclaim claims show ${intake.claim_id})`] : [];
    default:
      return [];
  }
}

export function claimLines(claim: ClaimResponse, now: number): string[] {
  const lines = [`Claim ${claim.id}`];
  const pad = (label: string) => `  ${label.padEnd(12)} `;
  lines.push(pad("Status:") + claim.status);
  lines.push(pad("Next:") + claim.next_action);
  lines.push(pad("Updated:") + `${formatTime(claim.updated_at)} (${relativeTime(claim.updated_at, now)})`);
  lines.push(pad("Draft:") + claim.intake_id);
  const notice = modeNotice(claim.mode);
  if (notice) lines.push(pad("Mode:") + notice);
  return lines;
}

/** A simple left-aligned table. */
export function table(headers: string[], rows: string[][]): string[] {
  const widths = headers.map((h, i) => Math.max(h.length, ...rows.map((r) => (r[i] ?? "").length)));
  const fmt = (cells: string[]) =>
    cells
      .map((c, i) => (i === cells.length - 1 ? c : c.padEnd(widths[i] ?? 0)))
      .join("  ")
      .trimEnd();
  return [fmt(headers), ...rows.map(fmt)];
}

export function cardLines(cards: CardResponse[]): string[] {
  return table(
    ["ID", "ISSUER", "CARD"],
    cards.map((c) => [String(c.id), c.issuer, c.name]),
  );
}
