/**
 * Types for the TourClaim connector API.
 *
 * Written by hand from openapi/connectors-v1.json (the live schema served at
 * /api/connectors/v1/openapi.json). test/schema.test.ts checks that the field
 * tables in fields.ts still match that file. The device authorization and key
 * endpoints are not in that schema yet; their shapes follow the contract in
 * README.md.
 */

export type Mode = "review" | "live";

export const REASON_CATEGORIES = [
  "ILLNESS",
  "INJURY",
  "DEATH_IN_FAMILY",
  "MILITARY_DEPLOYMENT",
  "WEATHER",
  "AIRLINE_CANCELLATION",
  "OTHER",
] as const;
export type ReasonCategory = (typeof REASON_CATEGORIES)[number];

export const OTHER_INSURANCE = ["YES", "NO", "UNSURE"] as const;
export type OtherInsurance = (typeof OTHER_INSURANCE)[number];

export const INTAKE_STATES = ["collecting", "needs_approval", "ready_to_submit", "submitted"] as const;
export type IntakeState = (typeof INTAKE_STATES)[number];

export const DOC_TYPES = ["receipt", "itinerary", "medical_note", "other"] as const;
export type DocType = (typeof DOC_TYPES)[number];

export const ATTACHMENT_CONTENT_TYPES = ["application/pdf", "image/jpeg", "image/png"] as const;
export type AttachmentContentType = (typeof ATTACHMENT_CONTENT_TYPES)[number];

export const EMAIL_PROVIDERS = ["gmail", "outlook", "user"] as const;
export type EmailProvider = (typeof EMAIL_PROVIDERS)[number];

export const SCOPES = ["intakes:write", "evidence:write", "claims:submit", "claims:read"] as const;
export type Scope = (typeof SCOPES)[number];

/** MedicalAnswers. Every answer is optional; null clears it. */
export interface MedicalAnswers {
  symptom_onset_date?: string | null;
  provider_seen?: boolean | null;
  provider_details?: string | null;
  prior_condition?: boolean | null;
  prior_condition_details?: string | null;
  stability_60day?: boolean | null;
  stability_60day_details?: string | null;
}

/** IntakeFields-Input. Omitted means unchanged; null clears the field. */
export interface IntakeFieldsInput {
  merchant_name?: string | null;
  booking_ref?: string | null;
  trip_date?: string | null;
  booking_amount?: string | number | null;
  refunded_amount?: string | number | null;
  currency?: "USD" | null;
  card_product_id?: number | null;
  reason_category?: ReasonCategory | null;
  narrative?: string | null;
  cancellation_policy?: string | null;
  other_insurance?: OtherInsurance | null;
  other_insurance_details?: string | null;
  medical?: MedicalAnswers | null;
}

/** IntakeFields-Output. Amounts come back as decimal strings. */
export interface IntakeFieldsOutput extends Omit<IntakeFieldsInput, "booking_amount" | "refunded_amount"> {
  booking_amount?: string | null;
  refunded_amount?: string | null;
}

export interface EvidenceSummary {
  id: string;
  kind: string;
  label: string;
}

export interface IntakeResponse {
  mode?: Mode;
  id: string;
  revision: number;
  fields: IntakeFieldsOutput;
  missing_fields: string[];
  next_questions: string[];
  evidence: EvidenceSummary[];
  state: IntakeState;
  review_url: string | null;
  evidence_upload_url?: string | null;
  claim_id: string | null;
  coverage_status?: "not_determined";
  medical_note_pathway: string;
}

export interface ClaimResponse {
  mode?: Mode;
  id: string;
  intake_id: string;
  status: string;
  next_action: string;
  updated_at: string;
}

export interface CardResponse {
  id: number;
  name: string;
  issuer: string;
  coverage_status?: "requires_review";
}

export interface StartIntake {
  fields?: IntakeFieldsInput;
}

export interface UpdateIntake {
  expected_revision: number;
  fields: IntakeFieldsInput;
}

export interface RevisionRequest {
  expected_revision: number;
}

export interface EmailEvidence {
  expected_revision: number;
  provider: EmailProvider;
  message_id: string;
  subject: string;
  sender: string;
  sent_at?: string | null;
  text: string;
  user_authorized_sharing: true;
}

export interface AttachmentEvidence {
  expected_revision: number;
  filename: string;
  content_type: AttachmentContentType;
  doc_type: DocType;
  content_base64: string;
  user_authorized_sharing: boolean;
}

/** GET /api/connectors/v1 (no auth). URLs are relative to the API origin. */
export interface ConnectorInfo {
  name: string;
  version: string;
  enabled: boolean;
  mode: Mode;
  openapi_url: string;
  connection_url: string;
  documentation_url: string;
}

/** GET /api/connectors/v1/key */
export interface KeyInfo {
  id: string;
  expires_at: string;
  scopes: string[];
  /** The traveler account the key belongs to. */
  account_email?: string | null;
}

/** POST /api/connectors/device/code */
export interface DeviceCodeResponse {
  device_code: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete?: string;
  expires_in: number;
  interval?: number;
}

/** POST /api/connectors/device/token, HTTP 200 */
export interface DeviceTokenSuccess {
  api_key: string;
  expires_at: string;
  scopes: string[];
  grant_id: string;
}

/** POST /api/connectors/device/token, HTTP 400 */
export interface DeviceTokenError {
  error: "authorization_pending" | "slow_down" | "access_denied" | "expired_token" | string;
  error_description?: string;
}

/** One item of a 422 response's detail list. */
export interface ValidationIssue {
  loc: Array<string | number>;
  type: string;
  msg: string;
}
