import { readFile } from "node:fs/promises";
import { UsageError } from "./errors.js";
import { OTHER_INSURANCE, REASON_CATEGORIES } from "./types.js";

export type FieldKind =
  | { kind: "string" }
  | { kind: "date" }
  | { kind: "decimal" }
  | { kind: "integer" }
  | { kind: "boolean" }
  | { kind: "enum"; values: readonly string[] }
  | { kind: "object" };

const STRING: FieldKind = { kind: "string" };
const DATE: FieldKind = { kind: "date" };
const DECIMAL: FieldKind = { kind: "decimal" };
const INTEGER: FieldKind = { kind: "integer" };
const BOOLEAN: FieldKind = { kind: "boolean" };

/** IntakeFields-Input, in the schema's order. */
export const INTAKE_FIELDS: Record<string, FieldKind> = {
  merchant_name: STRING,
  booking_ref: STRING,
  trip_date: DATE,
  booking_amount: DECIMAL,
  refunded_amount: DECIMAL,
  currency: { kind: "enum", values: ["USD"] },
  card_product_id: INTEGER,
  reason_category: { kind: "enum", values: REASON_CATEGORIES },
  narrative: STRING,
  cancellation_policy: STRING,
  other_insurance: { kind: "enum", values: OTHER_INSURANCE },
  other_insurance_details: STRING,
  medical: { kind: "object" },
};

/** MedicalAnswers, in the schema's order. Set as medical.<name>. */
export const MEDICAL_FIELDS: Record<string, FieldKind> = {
  symptom_onset_date: DATE,
  provider_seen: BOOLEAN,
  provider_details: STRING,
  prior_condition: BOOLEAN,
  prior_condition_details: STRING,
  stability_60day: BOOLEAN,
  stability_60day_details: STRING,
};

export type Fields = Record<string, unknown>;

function kindFor(key: string): FieldKind {
  const [top, sub, ...rest] = key.split(".");
  if (top === undefined || rest.length > 0) throw unknownField(key);
  if (sub === undefined) {
    const kind = INTAKE_FIELDS[top];
    if (!kind) throw unknownField(key);
    return kind;
  }
  if (top !== "medical") throw unknownField(key);
  const kind = MEDICAL_FIELDS[sub];
  if (!kind) throw unknownField(key);
  return kind;
}

function unknownField(key: string): UsageError {
  const names = [...Object.keys(INTAKE_FIELDS), ...Object.keys(MEDICAL_FIELDS).map((m) => `medical.${m}`)];
  return new UsageError(`Unknown field "${key}". Fields: ${names.join(", ")}.`);
}

function coerce(key: string, kind: FieldKind, raw: string): unknown {
  switch (kind.kind) {
    case "string":
      return raw;
    case "date":
      if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) throw new UsageError(`${key} must be a date like 2026-03-04.`);
      return raw;
    case "decimal":
      if (!/^\d+(\.\d{1,2})?$/.test(raw)) {
        throw new UsageError(`${key} must be an amount like 250.00, with no currency symbol or commas.`);
      }
      return raw;
    case "integer":
      if (!/^\d+$/.test(raw)) throw new UsageError(`${key} must be a whole number.`);
      return Number(raw);
    case "boolean": {
      const v = raw.toLowerCase();
      if (v === "true" || v === "yes") return true;
      if (v === "false" || v === "no") return false;
      throw new UsageError(`${key} must be true or false.`);
    }
    case "enum": {
      const v = raw.toUpperCase();
      if (!kind.values.includes(v)) throw new UsageError(`${key} must be one of: ${kind.values.join(", ")}.`);
      return v;
    }
    case "object":
      throw new UsageError(`Set ${key} one answer at a time (${key}.<name>=value) or as JSON (${key}:='{...}').`);
  }
}

function setPath(fields: Fields, key: string, value: unknown): void {
  const [top, sub] = key.split(".") as [string, string | undefined];
  if (sub === undefined) {
    fields[top] = value;
    return;
  }
  const current = fields[top];
  if (current === null) throw new UsageError(`${top} is being cleared and set in the same command.`);
  const obj = current && typeof current === "object" ? (current as Fields) : {};
  obj[sub] = value;
  fields[top] = obj;
}

/**
 * Parses key=value (typed by the schema) and key:=<json> (raw JSON) pairs.
 * medical answers are addressed as medical.<name>.
 */
export function parseAssignments(pairs: string[], into: Fields = {}): Fields {
  for (const pair of pairs) {
    const match = /^([A-Za-z0-9_.]+)(:?=)([\s\S]*)$/.exec(pair);
    if (!match) throw new UsageError(`Expected key=value or key:=<json>, got "${pair.length > 40 ? pair.slice(0, 40) + "..." : pair}".`);
    const [, key, op, raw] = match as unknown as [string, string, string, string];
    const kind = kindFor(key);
    if (op === ":=") {
      let value: unknown;
      try {
        value = JSON.parse(raw);
      } catch {
        throw new UsageError(`${key}:= needs a JSON value, such as 412, true, null or a quoted "string".`);
      }
      if (kind.kind === "object" && value !== null && (typeof value !== "object" || Array.isArray(value))) {
        throw new UsageError(`${key} must be a JSON object.`);
      }
      if (kind.kind === "object" && value && typeof value === "object") checkKeys(value as Fields, MEDICAL_FIELDS, `${key}.`);
      setPath(into, key, value);
    } else {
      if (raw === "") throw new UsageError(`${key}= has no value. To clear a field use --clear ${key}.`);
      setPath(into, key, coerce(key, kind, raw));
    }
  }
  return into;
}

/** Sets each named field to null, which the API treats as "clear". */
export function applyClears(names: string[], into: Fields): Fields {
  for (const name of names) {
    kindFor(name);
    const [top, sub] = name.split(".") as [string, string | undefined];
    if (sub === undefined ? into[top] !== undefined : (into[top] as Fields | null | undefined)?.[sub] !== undefined) {
      throw new UsageError(`${name} is both set and cleared.`);
    }
    setPath(into, name, null);
  }
  return into;
}

function checkKeys(obj: Fields, allowed: Record<string, FieldKind>, prefix: string): void {
  for (const key of Object.keys(obj)) {
    if (!(key in allowed)) throw unknownField(prefix + key);
  }
}

/** Reads a JSON object of fields from a file, or from stdin when path is "-". */
export async function readFieldsFile(path: string, readStdin: () => Promise<string>): Promise<Fields> {
  let text: string;
  try {
    text = path === "-" ? await readStdin() : await readFile(path, "utf8");
  } catch (error) {
    throw new UsageError(`Could not read ${path}: ${(error as Error).message}`);
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new UsageError(`${path} is not valid JSON.`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new UsageError(`${path} must hold a JSON object of intake fields, such as {"merchant_name": "Example Air"}.`);
  }
  const fields = value as Fields;
  checkKeys(fields, INTAKE_FIELDS, "");
  const medical = fields.medical;
  if (medical !== undefined && medical !== null) {
    if (typeof medical !== "object" || Array.isArray(medical)) throw new UsageError("medical must be a JSON object.");
    checkKeys(medical as Fields, MEDICAL_FIELDS, "medical.");
  }
  return fields;
}

/** Builds a fields object from a file, then pairs, then clears. */
export async function buildFields(options: {
  file?: string | undefined;
  pairs?: string[];
  clears?: string[];
  readStdin: () => Promise<string>;
}): Promise<Fields> {
  const fields: Fields = options.file ? await readFieldsFile(options.file, options.readStdin) : {};
  if (fields.medical && typeof fields.medical === "object") fields.medical = { ...(fields.medical as Fields) };
  parseAssignments(options.pairs ?? [], fields);
  applyClears(options.clears ?? [], fields);
  return fields;
}
