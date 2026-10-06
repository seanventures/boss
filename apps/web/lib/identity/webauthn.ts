/**
 * Passkeys in the browser (IDENTITY.md §4.5): turn the API's options (base64url fields) into
 * the browser's WebAuthn call, and the browser's answer back into JSON for /verify. The API
 * holds the challenge, checks the origin, the RP ID and the user handle; this file only carries.
 *
 * The identity stub (lib/identity/stub) answers options with `stub: true`; then no browser
 * prompt is shown and the stub's verify accepts. A real API never sends that field.
 */

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

export class PasskeyCancelled extends Error {
  constructor() { super("passkey_cancelled"); this.name = "PasskeyCancelled"; }
}

/** Ask the device for a passkey assertion. Throws PasskeyCancelled when the person backs out. */
export async function getAssertion(options: AssertionOptions): Promise<Record<string, unknown>> {
  if (options.stub) return { stub: true };
  if (!webauthnAvailable()) throw new Error("webauthn_unavailable");
  let cred: Credential | null;
  try {
    cred = await navigator.credentials.get({
      publicKey: {
        challenge: fromB64u(options.challenge),
        rpId: options.rpId,
        timeout: options.timeout,
        userVerification: options.userVerification ?? "required",
        allowCredentials: (options.allowCredentials ?? []).map((c) => ({ ...c, id: fromB64u(c.id) })),
      },
    });
  } catch (error) {
    if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError")) throw new PasskeyCancelled();
    throw error;
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
  };
}

/** Register a new passkey on this device. */
export async function createCredential(options: RegistrationOptions): Promise<Record<string, unknown>> {
  if (options.stub) return { stub: true };
  if (!webauthnAvailable()) throw new Error("webauthn_unavailable");
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
    if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError")) throw new PasskeyCancelled();
    throw error;
  }
  if (!cred) throw new PasskeyCancelled();
  const pk = cred as PublicKeyCredential;
  const r = pk.response as AuthenticatorAttestationResponse;
  return {
    id: pk.id, rawId: toB64u(pk.rawId), type: pk.type,
    response: { clientDataJSON: toB64u(r.clientDataJSON), attestationObject: toB64u(r.attestationObject), transports: r.getTransports?.() ?? [] },
  };
}
