/**
 * A deliberately small .eml reader: headers (Subject, From, Date, Message-ID)
 * and the first text/plain body part. It handles folded headers, RFC 2047
 * encoded words, multipart nesting, base64 and quoted-printable. Anything it
 * cannot read is reported rather than guessed.
 */

export interface ParsedEmail {
  subject: string | null;
  from: string | null;
  /** ISO 8601, or null when there is no readable Date header. */
  date: string | null;
  messageId: string | null;
  /** The first text/plain part, or null when the message has none. */
  text: string | null;
}

type Headers = Map<string, string>;

const MAX_DEPTH = 10;

export function parseEml(raw: Uint8Array): ParsedEmail {
  // latin1 maps each byte to one character, so byte offsets survive until
  // each part is decoded with its own charset.
  const source = Buffer.from(raw).toString("latin1");
  const { head, body } = splitMessage(source);
  const headers = parseHeaders(head);
  const dateHeader = headers.get("date");
  let date: string | null = null;
  if (dateHeader) {
    const ms = Date.parse(dateHeader);
    if (!Number.isNaN(ms)) date = new Date(ms).toISOString();
  }
  const messageId = headers.get("message-id")?.trim().replace(/^<|>$/g, "").trim() || null;
  return {
    subject: headers.has("subject") ? decodeHeader(headers.get("subject") ?? "") : null,
    from: headers.has("from") ? decodeHeader(headers.get("from") ?? "") : null,
    date,
    messageId,
    text: findTextPart(headers, body, 0),
  };
}

function splitMessage(source: string): { head: string; body: string } {
  const match = /\r?\n\r?\n/.exec(source);
  if (!match) return { head: source, body: "" };
  return { head: source.slice(0, match.index), body: source.slice(match.index + match[0].length) };
}

function parseHeaders(head: string): Headers {
  const headers: Headers = new Map();
  const unfolded = head.replace(/\r?\n[ \t]+/g, " ");
  for (const line of unfolded.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const name = line.slice(0, colon).trim().toLowerCase();
    if (!headers.has(name)) headers.set(name, line.slice(colon + 1).trim());
  }
  return headers;
}

function decodeBytes(bytes: Uint8Array, charset: string | undefined): string {
  const label = (charset ?? "utf-8").trim().toLowerCase() || "utf-8";
  try {
    return new TextDecoder(label).decode(bytes);
  } catch {
    return new TextDecoder("utf-8").decode(bytes);
  }
}

/** Raw 8-bit header text is usually UTF-8; encoded words carry their charset. */
export function decodeHeader(value: string): string {
  const raw = Buffer.from(value, "latin1");
  let text = value;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  } catch {
    // Not UTF-8; keep the latin1 reading.
  }
  return text
    .replace(/(=\?[^?]+\?[BbQq]\?[^?]*\?=)\s+(?==\?)/g, "$1")
    .replace(/=\?([^?]+)\?([BbQq])\?([^?]*)\?=/g, (_whole, charset: string, encoding: string, data: string) => {
      const bytes =
        encoding.toUpperCase() === "B"
          ? Buffer.from(data, "base64")
          : Buffer.from(data.replace(/_/g, " ").replace(/=([0-9A-Fa-f]{2})/g, (_m, hex: string) => String.fromCharCode(parseInt(hex, 16))), "latin1");
      return decodeBytes(bytes, charset.split("*")[0]);
    });
}

function parseContentType(value: string | undefined): { type: string; params: Map<string, string> } {
  const params = new Map<string, string>();
  if (!value) return { type: "text/plain", params };
  const [type = "", ...rest] = value.split(";");
  for (const part of rest) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const key = part.slice(0, eq).trim().toLowerCase();
    let val = part.slice(eq + 1).trim();
    if (val.startsWith('"') && val.endsWith('"') && val.length >= 2) val = val.slice(1, -1).replace(/\\(.)/g, "$1");
    params.set(key, val);
  }
  return { type: type.trim().toLowerCase() || "text/plain", params };
}

function decodeTransfer(body: string, encoding: string | undefined): Uint8Array {
  const enc = (encoding ?? "").trim().toLowerCase();
  if (enc === "base64") return Buffer.from(body.replace(/\s+/g, ""), "base64");
  if (enc === "quoted-printable") {
    const decoded = body
      .replace(/=\r?\n/g, "")
      .replace(/=([0-9A-Fa-f]{2})/g, (_m, hex: string) => String.fromCharCode(parseInt(hex, 16)));
    return Buffer.from(decoded, "latin1");
  }
  return Buffer.from(body, "latin1");
}

function splitMultipart(body: string, boundary: string): string[] {
  const delimiter = `--${boundary}`;
  const parts: string[] = [];
  let current: string[] | null = null;
  for (const line of body.split(/\r?\n/)) {
    const trimmed = line.trimEnd();
    if (trimmed === `${delimiter}--`) {
      if (current) parts.push(current.join("\n"));
      return parts;
    }
    if (trimmed === delimiter) {
      if (current) parts.push(current.join("\n"));
      current = [];
      continue;
    }
    current?.push(line);
  }
  if (current) parts.push(current.join("\n"));
  return parts;
}

function findTextPart(headers: Headers, body: string, depth: number): string | null {
  if (depth > MAX_DEPTH) return null;
  const { type, params } = parseContentType(headers.get("content-type"));
  if (type.startsWith("multipart/")) {
    const boundary = params.get("boundary");
    if (!boundary) return null;
    for (const part of splitMultipart(body, boundary)) {
      const split = splitMessage(part);
      const text = findTextPart(parseHeaders(split.head), split.body, depth + 1);
      if (text !== null) return text;
    }
    return null;
  }
  if (type !== "text/plain") return null;
  if (/^\s*attachment/i.test(headers.get("content-disposition") ?? "")) return null;
  const text = decodeBytes(decodeTransfer(body, headers.get("content-transfer-encoding")), params.get("charset"));
  return text.replace(/\r\n/g, "\n").replace(/\s+$/, "");
}
