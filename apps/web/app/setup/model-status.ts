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
import { localWith } from "../../lib/hydration-state";
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
  if (row) { const pill = limitPill(row.pill); return { label: pill.label, tip: pill.tip, pill, found: null }; }
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
/** A plan limit's reset, ONE formatter for the pill and the sentence beside it, so they can
 *  never disagree: both are drawn in the same render, before hydration (UTC, the server's
 *  paint) and after (the viewer's own clock). */
export function limitClock(at: string | null | undefined): string {
  return at ? localWith(at, { hour: "numeric", minute: "2-digit" }) : "";
}
function limitDay(at: string): string {
  return localWith(at, { weekday: "short" });
}

/** The pill as drawn: a Limited pill's time is the viewer's, from `at` (the server's label
 *  carries its own clock, UTC in a container). Any other pill is unchanged. */
export function limitPill<T extends Pill>(pill: T): T {
  if (pill.state !== "rate_limited" || !pill.at) return pill;
  const clock = limitClock(pill.at);
  if (!clock) return pill;
  const swap = (text: string) => text.replace(/\b\d{1,2}:\d{2}(?:\s?[AP]M)?/gi, clock);
  const base = pill.label.split(" · ")[0] || "Limited";
  return { ...pill, label: `${base} · ${clock}`, tip: swap(pill.tip) };
}

export function needsSentence(pill: Pill): string {
  if (pill.state === "no_credit") return "Top up at the provider.";
  // A PLAN LIMIT IS NOT AN ERROR: it reopens by itself, and the pill's `at` says when (the
  // viewer's clock). The provider's own sentence rides in the tooltip (the pill's tip).
  if (pill.state === "rate_limited") {
    return pill.at ? `Limit resets ${limitDay(pill.at)} ${limitClock(pill.at)}.` : "Plan limit reached.";
  }
  if (pill.action === "fix_link") return "Fix it at the provider.";
  return NEEDS[pill.action ?? ""] ?? "Not usable yet.";
}
