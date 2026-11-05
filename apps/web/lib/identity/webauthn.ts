/**
 * Passkeys in the browser (IDENTITY.md §4.5): turn the API's options (base64url fields) into
 * the browser's WebAuthn call, and the browser's answer back into JSON for /verify. The API
 * holds the challenge, checks the origin, the RP ID and the user handle; this file only carries.
 *
 * The identity stub (lib/identity/stub) answers options with `stub: true`; then no browser
 * prompt is shown and the stub's verify accepts. A real API never sends that field.
 */

import { PasskeyFailure } from "./passkey-support";

export { PasskeyFailure };

type B64 = string;
export type AssertionOptions = {
  stub?: boolean;
  challenge: B64;
  rpId?: string;
  timeout?: number;
  userVerification?: UserVerificationRequirement;
  allowCredentials?: { id: B64; type: "public-key"; transports?: AuthenticatorTransport[] }[];
};
export type RegistrationOptions = {
  stub?: boolean;
  challenge: B64;
  rp: { id?: string; name: string };
  user: { id: B64; name: string; displayName: string };
  pubKeyCredParams: { type: "public-key"; alg: number }[];
  timeout?: number;
  excludeCredentials?: { id: B64; type: "public-key" }[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
};

export function webauthnAvailable(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential === "function" && !!navigator.credentials;
}

/**
 * Whether this browser offers passkeys in the email field's autofill without a modal
 * (conditional mediation, autocomplete="username webauthn"). False wherever it cannot be asked.
 */
export async function conditionalMediationAvailable(): Promise<boolean> {
  if (!webauthnAvailable()) return false;
  const check = (window.PublicKeyCredential as unknown as { isConditionalMediationAvailable?: () => Promise<boolean> })
    .isConditionalMediationAvailable;
  if (typeof check !== "function") return false;
  try { return (await check.call(window.PublicKeyCredential)) === true; } catch { return false; }
}

function fromB64u(value: B64): ArrayBuffer {
  const pad = "=".repeat((4 - (value.length % 4)) % 4);
  const raw = atob((value + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

function toB64u(buffer: ArrayBuffer | null | undefined): B64 | null {
  if (!buffer) return null;
  const bytes = new Uint8Array(buffer);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * The API answers `{"publicKey": {...}}` (S1, the WebAuthn JSON shape navigator.credentials
 * takes); the stub answered the bare options. Both are accepted, so every caller passes the
 * API's answer through untouched.
 */
export type Wrapped<T> = T | { publicKey: T };
export function unwrapOptions<T>(given: Wrapped<T>): T {
  return given && typeof given === "object" && "publicKey" in given && (given as { publicKey?: unknown }).publicKey
    ? (given as { publicKey: T }).publicKey
    : given as T;
}

function extensions(pk: PublicKeyCredential): Record<string, unknown> {
  try { return (pk.getClientExtensionResults?.() ?? {}) as Record<string, unknown>; } catch { return {}; }
}

/** The person backed out, or the prompt timed out (NotAllowedError / AbortError). */
export class PasskeyCancelled extends PasskeyFailure {
  constructor(reason = "NotAllowedError") { super(reason); this.name = "PasskeyCancelled"; }
}

/** Every browser refusal carries its DOMException name, so the screen can say WHY (passkey-support). */
function failure(error: unknown): unknown {
  if (typeof DOMException !== "undefined" && error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "AbortError") return new PasskeyCancelled(error.name);
    return new PasskeyFailure(error.name, error.message);
  }
  if (error instanceof TypeError) return new PasskeyFailure("TypeError", error.message);
  return error;
}

/**
 * Ask the device for a passkey assertion. Throws PasskeyCancelled when the person backs out.
 * `conditional`: no modal — the browser offers its passkeys in the email field's autofill and
 * answers only when the person picks one (abort it with `signal` before any other request).
 */
export async function getAssertion(given: Wrapped<AssertionOptions>, how: { conditional?: boolean; signal?: AbortSignal } = {}): Promise<Record<string, unknown>> {
  const options = unwrapOptions(given);
  if (options.stub && !how.conditional) return { stub: true };
  if (!webauthnAvailable()) throw new PasskeyFailure("webauthn_unavailable");
  let cred: Credential | null;
  try {
    cred = await navigator.credentials.get({
      ...(how.conditional ? { mediation: "conditional" as CredentialMediationRequirement } : {}),
      ...(how.signal ? { signal: how.signal } : {}),
      publicKey: {
        challenge: fromB64u(options.challenge),
        rpId: options.rpId,
        timeout: options.timeout,
        userVerification: options.userVerification ?? "required",
        allowCredentials: (options.allowCredentials ?? []).map((c) => ({ ...c, id: fromB64u(c.id) })),
      },
    });
  } catch (error) {
    throw failure(error);
  }
  if (!cred) throw new PasskeyCancelled();
  const pk = cred as PublicKeyCredential;
  const r = pk.response as AuthenticatorAssertionResponse;
  return {
    id: pk.id, rawId: toB64u(pk.rawId), type: pk.type,
    response: {
      clientDataJSON: toB64u(r.clientDataJSON), authenticatorData: toB64u(r.authenticatorData),
      signature: toB64u(r.signature), userHandle: toB64u(r.userHandle),
    },
    clientExtensionResults: extensions(pk), authenticatorAttachment: pk.authenticatorAttachment ?? null,
  };
}

/** Register a new passkey on this device. */
export async function createCredential(given: Wrapped<RegistrationOptions>): Promise<Record<string, unknown>> {
  const options = unwrapOptions(given);
  if (options.stub) return { stub: true };
  if (!webauthnAvailable()) throw new PasskeyFailure("webauthn_unavailable");
  let cred: Credential | null;
  try {
    cred = await navigator.credentials.create({
      publicKey: {
        ...options,
        challenge: fromB64u(options.challenge),
        user: { ...options.user, id: fromB64u(options.user.id) },
        excludeCredentials: (options.excludeCredentials ?? []).map((c) => ({ ...c, id: fromB64u(c.id) })),
      },
    });
  } catch (error) {
    throw failure(error);
  }
  if (!cred) throw new PasskeyCancelled();
  const pk = cred as PublicKeyCredential;
  const r = pk.response as AuthenticatorAttestationResponse;
  return {
    id: pk.id, rawId: toB64u(pk.rawId), type: pk.type,
    response: { clientDataJSON: toB64u(r.clientDataJSON), attestationObject: toB64u(r.attestationObject), transports: r.getTransports?.() ?? [] },
    clientExtensionResults: extensions(pk), authenticatorAttachment: pk.authenticatorAttachment ?? null,
  };
}
