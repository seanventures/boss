/**
 * ONE ANSWER to "what is this provider's state, and what does it need next?" for the Models
 * section: its rows, its Connect/Sign in buttons and the Add a model drawer's provider chips all
 * read it, so a chip can never say "Sign in" beside a row that says "Working" (2026-10-25).
 *
 * The row's pill is the server's last real proof (setup_view.source_state) and wins. The CLI's
 * own "signed in" (detect) only decides the NEXT step: a signed-in CLI is offered Connect, which
 * proves it without signing anything out; Sign in (which replaces the login) is never the
 * default while the CLI says it is signed in.
 */
import type { Detected, ModelEntry, Pill, SetupView } from "../../lib/setup-types";

/** States whose last proof refused the login or key. */
const REFUSED = new Set(["login_expired", "key_rejected"]);

export const refusedPill = (pill: Pill | null | undefined): boolean => !!pill && REFUSED.has(pill.state);

/** The provider's Models row: a working one first, else any. */
export function providerRow(view: SetupView, provider: string): ModelEntry | undefined {
  const rows = view.models.filter((m) => m.provider === provider);
  return rows.find((m) => m.pill.tone === "green") ?? rows[0];
}

export function detectedOf(view: SetupView, provider: string): Detected | undefined {
  return view.detected.find((d) => d.provider === provider);
}

/** The CLI on the BOSS server says it is signed in. */
export function cliSignedIn(view: SetupView, provider: string): boolean {
  return detectedOf(view, provider)?.found === "signed_in";
}

/**
 * What a subscription BOSS signs in itself needs next, or null when its row is working.
 * `connect`: the CLI is signed in — prove it (no sign-out). `sign_in`: run the provider's login.
 */
export function subscriptionNext(view: SetupView, provider: string): "connect" | "sign_in" | null {
  const row = providerRow(view, provider);
  if (row && row.pill.tone === "green") return null;
  return cliSignedIn(view, provider) ? "connect" : "sign_in";
}

const FOUND_LABEL: Record<string, string> = {
  signed_in: "Signed in", found: "Found", needs_sign_in: "Not signed in", not_on_server: "Not on server",
};
const FOUND_TIP: Record<string, string> = {
  signed_in: "Signed in on the BOSS server. Connect checks it.", found: "Found on this server.",
  needs_sign_in: "Sign in below. Runs on the BOSS server.",
  not_on_server: "Install it on the BOSS server first.",
};

/** A provider chip's status: the row's own pill when it has one, else what the server found. */
export type ChipStatus = { label: string; tip: string; pill: Pill | null; found: string | null };
export function chipStatus(view: SetupView, provider: string): ChipStatus | null {
  const row = providerRow(view, provider);
  if (row) return { label: row.pill.label, tip: row.pill.tip, pill: row.pill, found: null };
  const d = detectedOf(view, provider);
  if (!d || !FOUND_LABEL[d.found]) return null;
  return { label: FOUND_LABEL[d.found], tip: FOUND_TIP[d.found], pill: null, found: d.found };
}

/** One plain sentence (six words at most) for a row whose pill needs something; the server's
 *  full tip rides in its tooltip. */
const NEEDS: Record<string, string> = {
  sign_in: "Sign in to use it.",
  replace: "Replace the key to use it.",
  default_model: "Pick a model it offers.",
  budget: "Paused by the budget.",
  test: "Test again when it's reachable.",
  connect: "Connect to use it.",
};
export function needsSentence(pill: Pill): string {
  if (pill.state === "no_credit") return "Top up at the provider.";
  if (pill.action === "fix_link") return "Fix it at the provider.";
  return NEEDS[pill.action ?? ""] ?? "Not usable yet.";
}
