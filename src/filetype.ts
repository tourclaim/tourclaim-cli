import type { AttachmentContentType } from "./types.js";

/** The API's limit on an attachment's decoded size. */
export const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;

const SIGNATURES: Array<[AttachmentContentType, number[]]> = [
  ["application/pdf", [0x25, 0x50, 0x44, 0x46, 0x2d]], // %PDF-
  ["image/jpeg", [0xff, 0xd8, 0xff]],
  ["image/png", [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]],
];

/**
 * Detects PDF, JPEG or PNG from the first bytes, the same check the API makes.
 * Returns null for anything else. This is not a full parse or a malware scan.
 */
export function detectContentType(bytes: Uint8Array): AttachmentContentType | null {
  for (const [type, signature] of SIGNATURES) {
    if (bytes.length >= signature.length && signature.every((b, i) => bytes[i] === b)) return type;
  }
  return null;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
}
