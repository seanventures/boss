/**
 * Shapes the auth screens and My account read beyond lib/identity/types.ts. SEAMS with S1 (and S3
 * for connections): each is the console's reading of IDENTITY.md §10 until the OpenAPI lands.
 */
import type { Provider } from "./types";

/** GET /v1/auth/invitations/{token} — public preview of an open invitation (404 when used/expired). */
export interface InvitationPreview {
  org: string;
  inviter: string;
  email: string;
  roles: string[];
  expires_at: string | null;
  sso: { id: string; name: string; preset: Provider["preset"] }[];
}

/** POST /v1/identity/me/mfa/totp — shown ONCE. `qr_svg` is a seam: S1 renders the QR server-side. */
export interface TotpEnrolment {
  otpauth_uri: string;
  secret: string;
  qr_svg?: string | null;
}

/** POST /v1/identity/me/recovery-codes — the ten codes, shown ONCE. */
export interface RecoveryCodes {
  codes: string[];
}

/** GET /v1/identity/me/identities — linked SSO identities (never the subject in full). */
export interface LinkedIdentity {
  id: string;
  provider: string;
  preset: Provider["preset"];
  label: string;
  linked_at: string;
}
