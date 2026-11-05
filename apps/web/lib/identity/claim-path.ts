/**
 * WHICH PROOF THIS CLAIM USES, AND WHICH ORIGIN IT BINDS (round 3, W1; J2, J14).
 *
 * /v1/auth/options names ONE `path` for the page (network > shared password > setup link), and
 * `paths`, every proof this browser may use. A page opened from the install's setup link holds the
 * token in its fragment (`#t=`, which never reaches a server), so the API cannot know it is there:
 * on an install that also has a shared console password, `path` said legacy_password and the page
 * asked for a password the person was never given. The fragment wins whenever the API accepts a
 * setup link here.
 *
 * The network path binds the origin the BROWSER is on. The API names it when it can (the browser's
 * Origin, or the host a trusted proxy forwarded); when it says null, the page's own origin is the
 * one being claimed — never an internal address.
 */
import type { AuthOptions } from "./types";

type Claim = NonNullable<AuthOptions["claim"]>;
export type ClaimPath = Claim["path"];

/** The proofs the API accepts here. An API that does not list them accepts the one it named. */
function accepted(claim: Claim): string[] {
  if (Array.isArray(claim.paths)) return claim.paths;
  return [claim.path === "network_bridge" ? "network" : claim.path];
}

/**
 * The claim page never asks for the shared console password (operator, 2026-11-05: "asking for
 * password for the first sign up page and claiming your instance" is wrong). Compose always sets
 * one, so a fresh install opened without `#t=` asked for a password nobody was shown. Wherever the
 * API accepts a setup link, the setup link is the proof; the page says how to get one. The API
 * still accepts the shared password (older clients), the console just never offers it.
 */
export function claimPathFor(claim: Claim, _fragmentToken: string): ClaimPath {
  if (accepted(claim).includes("setup_token") && claim.path === "legacy_password") return "setup_token";
  return claim.path;
}

export function claimOrigin(claim: Claim, pageOrigin: string | null): string | null {
  return claim.origin || pageOrigin || null;
}

/** The proof field of POST /v1/auth/setup/claim for this path. */
export function claimProof(path: ClaimPath, input: { token: string; legacy: string; origin: string | null }): Record<string, string> {
  if (path === "setup_token") return { setup_token: input.token };
  if (path === "legacy_password") return { legacy_password: input.legacy };
  return input.origin ? { confirm_origin: input.origin } : {};
}
