/**
 * WHAT THE SIGN-IN PAGE OFFERS, AND WHY NOT (no fake buttons).
 *
 * The page always has its full shape: the four social providers, a passkey button and the three
 * email methods. Each one is live only when `/v1/auth/options` (and, for passkeys, this browser's
 * own address) says it can work; otherwise it is shown disabled with the exact words that turn it
 * on. A plain module so the page and its tests read the same rules.
 */
import type { AuthOptions } from "./types";
import { emailSignInOffered } from "./email-sign-in";
import { passkeyBlockWords } from "./passkey-support";

export type SocialPreset = "google" | "github" | "microsoft" | "apple";
export const SOCIAL: { preset: SocialPreset; name: string }[] = [
  { preset: "google", name: "Google" },
  { preset: "github", name: "GitHub" },
  { preset: "microsoft", name: "Microsoft" },
  { preset: "apple", name: "Apple" },
];

export const WHERE_SSO_IS_SET = "an Owner turns it on in Identity › Sign-in & SSO";

/** One provider button: live (an SSO start URL) or not (why). */
export type ProviderOffer = {
  key: string;
  name: string;
  preset: AuthOptions["providers"][number]["preset"];
  href: string | null;
  why: string | null;
};

export function ssoStartHref(id: string, next: string): string {
  return `/v1/auth/sso/${encodeURIComponent(id)}/start?next=${encodeURIComponent(next)}`;
}

/**
 * The four social squares in a fixed order (the first configured provider of each preset makes
 * it live), then every other configured provider — OIDC or SAML — in the API's order.
 */
export function providerOffers(providers: AuthOptions["providers"], next: string): ProviderOffer[] {
  const used = new Set<string>();
  const social = SOCIAL.map(({ preset, name }): ProviderOffer => {
    const p = providers.find((x) => x.preset === preset);
    if (p) used.add(p.id);
    return p
      ? { key: p.id, name: p.name || name, preset, href: ssoStartHref(p.id, next), why: null }
      : { key: `off-${preset}`, name, preset, href: null, why: `${name} sign-in isn't set up · ${WHERE_SSO_IS_SET}` };
  });
  const rest = providers.filter((p) => !used.has(p.id))
    .map((p): ProviderOffer => ({ key: p.id, name: p.name, preset: p.preset, href: ssoStartHref(p.id, next), why: null }));
  return [...social, ...rest];
}

/**
 * Why "Sign in with a passkey" can't work here, or null when it can. `where` is the page's own
 * address (null on the server); `webauthn` is whether this browser has passkeys at all.
 */
export function passkeyWhyNot(options: Pick<AuthOptions, "passkey" | "passkey_reason">,
  where: { hostname?: string; protocol?: string } | null, webauthn: boolean): string | null {
  const blocked = passkeyBlockWords(options.passkey_reason, where);
  if (blocked) return blocked;
  if (options.passkey_reason === "public_origin_unset") return "Passkeys need BOSS's public address. It's set when an Owner claims BOSS.";
  if (options.passkey === false) return "Passkeys aren't set up on this BOSS yet";
  if (where && !webauthn) return "This browser has no passkeys";
  return null;
}

export type Method = "password" | "magic" | "code";

/** Why an email method is off, or null when it is offered. Password always is. */
export function methodWhyNot(method: Method, options: Pick<AuthOptions, "smtp" | "public_origin">): string | null {
  if (method === "password" || emailSignInOffered(options)) return null;
  const what = method === "magic" ? "Magic links" : "Email codes";
  if (!options.smtp) return `${what} need email (SMTP) set up`;
  return `${what} need BOSS's public address set`;
}
