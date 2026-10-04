import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { describeErrorBody, retryAfterSeconds, sentences } from "../src/api.js";
import { normalizeApiUrl, resolveApiUrl, userAgent } from "../src/config.js";
import { decodeHeader, parseEml } from "../src/eml.js";
import { applyClears, parseAssignments } from "../src/fields.js";
import { detectContentType } from "../src/filetype.js";
import { formatTime, parseServerTime } from "../src/format.js";
import { Output } from "../src/output.js";

describe("fields", () => {
  it("types key=value pairs by the schema", () => {
    assert.deepEqual(
      parseAssignments([
        "merchant_name=Example Air",
        "trip_date=2026-03-04",
        "booking_amount=250",
        "refunded_amount=0.00",
        "currency=usd",
        "card_product_id=412",
        "reason_category=Weather",
        "other_insurance=no",
        "medical.provider_seen=No",
        "medical.prior_condition=true",
        "narrative=a=b and c",
      ]),
      {
        merchant_name: "Example Air",
        trip_date: "2026-03-04",
        booking_amount: "250",
        refunded_amount: "0.00",
        currency: "USD",
        card_product_id: 412,
        reason_category: "WEATHER",
        other_insurance: "NO",
        medical: { provider_seen: false, prior_condition: true },
        narrative: "a=b and c",
      },
    );
  });

  it("passes := values through as JSON", () => {
    assert.deepEqual(parseAssignments(["card_product_id:=412", "medical:={\"provider_seen\":true}", "medical.prior_condition:=null", "narrative:=\"x\""]), {
      card_product_id: 412,
      medical: { provider_seen: true, prior_condition: null },
      narrative: "x",
    });
    assert.throws(() => parseAssignments(["medical:=[1]"]), /JSON object/);
    assert.throws(() => parseAssignments(["medical:={\"diagnosis\":\"x\"}"]), /Unknown field "medical.diagnosis"/);
    assert.throws(() => parseAssignments(["card_product_id:=nope"]), /JSON value/);
  });

  it("clears with null and catches conflicts", () => {
    assert.deepEqual(applyClears(["narrative", "medical.provider_details"], {}), { narrative: null, medical: { provider_details: null } });
    assert.deepEqual(applyClears(["medical"], {}), { medical: null });
    assert.throws(() => applyClears(["narrative"], { narrative: "x" }), /both set and cleared/);
    assert.throws(() => parseAssignments(["medical.provider_seen=true"], { medical: null }), /cleared and set/);
  });

  it("rejects unknown and malformed values", () => {
    for (const pair of ["unknown=1", "medical.unknown=1", "medical.provider_seen.x=1", "card_product_id=12a", "booking_amount=1,250.00", "trip_date=2026/03/04", "medical.provider_seen=maybe", "currency=EUR", "nonsense"]) {
      assert.throws(() => parseAssignments([pair]), pair);
    }
  });
});

describe("filetype", () => {
  it("detects PDF, JPEG and PNG by their first bytes only", () => {
    assert.equal(detectContentType(Buffer.from("%PDF-1.7\n")), "application/pdf");
    assert.equal(detectContentType(Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0])), "image/jpeg");
    assert.equal(detectContentType(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0])), "image/png");
    assert.equal(detectContentType(Buffer.from(" %PDF-1.7")), null);
    assert.equal(detectContentType(Buffer.from("GIF89a")), null);
    assert.equal(detectContentType(Buffer.alloc(0)), null);
  });
});

describe("eml", () => {
  it("reads headers, decodes encoded words and the text/plain part", () => {
    const raw = [
      "Subject: =?iso-8859-1?Q?R=E9servation_annul=E9e?=",
      " =?utf-8?B?IOKckw==?=",
      "From: =?utf-8?Q?Caf=C3=A9_Tours?= <ops@cafe.test>",
      "Date: Tue, 3 Mar 2026 09:05:00 -0500",
      "Message-ID: <abc@cafe.test>",
      "Content-Type: multipart/mixed; boundary=outer",
      "",
      "preamble",
      "--outer",
      "Content-Type: multipart/alternative; boundary=\"inner\"",
      "",
      "--inner",
      "Content-Type: text/plain; charset=utf-8",
      "Content-Transfer-Encoding: base64",
      "",
      Buffer.from("Votre réservation est annulée.\nMerci.").toString("base64"),
      "--inner",
      "Content-Type: text/html",
      "",
      "<p>no</p>",
      "--inner--",
      "--outer",
      "Content-Type: text/plain",
      "Content-Disposition: attachment; filename=x.txt",
      "",
      "attachment text",
      "--outer--",
    ].join("\n");
    const parsed = parseEml(Buffer.from(raw, "utf8"));
    assert.equal(parsed.subject, "Réservation annulée ✓");
    assert.equal(parsed.from, "Café Tours <ops@cafe.test>");
    assert.equal(parsed.date, "2026-03-03T14:05:00.000Z");
    assert.equal(parsed.messageId, "abc@cafe.test");
    assert.equal(parsed.text, "Votre réservation est annulée.\nMerci.");
  });

  it("handles a single-part message, raw UTF-8 headers and no text part", () => {
    const simple = parseEml(Buffer.from("Subject: Booking café\r\nFrom: a@b.test\r\n\r\nHello\r\nthere\r\n", "utf8"));
    assert.equal(simple.subject, "Booking café");
    assert.equal(simple.text, "Hello\nthere");
    assert.equal(simple.date, null);
    const htmlOnly = parseEml(Buffer.from("Content-Type: text/html\n\n<p>x</p>"));
    assert.equal(htmlOnly.text, null);
    assert.equal(decodeHeader("plain"), "plain");
  });
});

describe("config", () => {
  it("normalizes API URLs and refuses unsafe ones", () => {
    assert.equal(normalizeApiUrl("https://app.getcopernican.com/"), "https://app.getcopernican.com");
    assert.equal(normalizeApiUrl("https://app.getcopernican.com/api/connectors/v1"), "https://app.getcopernican.com");
    assert.equal(normalizeApiUrl("https://staging.example.com/prefix/"), "https://staging.example.com/prefix");
    assert.equal(normalizeApiUrl("http://localhost:4010"), "http://localhost:4010");
    assert.equal(normalizeApiUrl("http://127.0.0.1:4010"), "http://127.0.0.1:4010");
    assert.throws(() => normalizeApiUrl("http://app.getcopernican.com"), /plain http/);
    assert.throws(() => normalizeApiUrl("ftp://x"), /https/);
    assert.throws(() => normalizeApiUrl("https://user:pw@x.test"), /user name or password/);
    assert.throws(() => normalizeApiUrl("not a url"), /valid URL/);
  });

  it("prefers --api-url, then TOURCLAIM_API_URL, then the default", () => {
    assert.equal(resolveApiUrl(undefined, {}), "https://app.getcopernican.com");
    assert.equal(resolveApiUrl(undefined, { TOURCLAIM_API_URL: "https://env.test" }), "https://env.test");
    assert.equal(resolveApiUrl("https://flag.test", { TOURCLAIM_API_URL: "https://env.test" }), "https://flag.test");
  });

  it("builds the User-Agent", () => {
    assert.equal(userAgent("darwin", "arm64", "22.4.0"), "tourclaim-cli/0.1.0 (darwin arm64; node 22.4.0)");
  });
});

describe("api helpers", () => {
  it("reads Retry-After as seconds or a date", () => {
    assert.equal(retryAfterSeconds("7"), 7);
    assert.equal(retryAfterSeconds(null), 5);
    assert.equal(retryAfterSeconds(new Date(Date.now() + 30_000).toUTCString()) >= 29, true);
  });

  it("describes every error body shape", () => {
    assert.equal(describeErrorBody(409, { detail: "Intake changed" }), "Intake changed");
    assert.equal(
      describeErrorBody(422, { detail: [{ loc: ["body", "fields", "trip_date"], type: "date_from_datetime_parsing", msg: "Invalid field value" }] }),
      "The API rejected these values: fields.trip_date (date_from_datetime_parsing).",
    );
    assert.equal(describeErrorBody(429, { error: "Rate limit exceeded: 60 per 1 minute" }), "Rate limit exceeded: 60 per 1 minute");
    assert.equal(describeErrorBody(400, { error: "access_denied", error_description: "Declined" }), "Declined");
    assert.equal(describeErrorBody(502, "<html>bad gateway</html>"), "The API answered HTTP 502.");
    assert.equal(sentences("One", "Two.", undefined, "Three:"), "One. Two. Three:");
  });

  it("treats zone-less server times as UTC", () => {
    assert.equal(parseServerTime("2026-11-03T18:00:00")?.toISOString(), "2026-11-03T18:00:00.000Z");
    assert.equal(parseServerTime("2026-11-03T18:00:00+02:00")?.toISOString(), "2026-11-03T16:00:00.000Z");
    assert.equal(formatTime("2026-11-03T18:00:00"), "2026-11-03 18:00 UTC");
  });
});

describe("output", () => {
  it("redacts anything that looks like a key, in every stream", () => {
    let out = "";
    let err = "";
    const o = new Output((t) => (out += t), (t) => (err += t), false);
    o.addSecret("custom-secret-value");
    o.line("key tc_muse_abcDEF123_-xyz and custom-secret-value");
    o.info("tc_muse_zzzzzzzz");
    o.error(new Error("leak tc_muse_qqqqqqqq"));
    assert.equal(out, "key tc_muse_[redacted] and [redacted]\n");
    assert.ok(!err.includes("zzzz") && !err.includes("qqqq"));
  });
});

describe("relative time", () => {
  it("reads naturally at every scale", async () => {
    const { relativeTime } = await import("../src/format.js");
    const now = Date.parse("2026-10-04T12:00:00Z");
    assert.equal(relativeTime("2026-10-04T11:59:50Z", now), "just now");
    assert.equal(relativeTime("2026-10-04T11:45:00Z", now), "15 minutes ago");
    assert.equal(relativeTime("2026-10-04T14:00:00Z", now), "in 2 hours");
    assert.equal(relativeTime("2026-11-03T11:59:00", now), "in 30 days");
    assert.equal(relativeTime("2026-10-05T12:00:00Z", now), "in 1 day");
  });
});
