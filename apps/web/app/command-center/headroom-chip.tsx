"use client";

/**
 * The Command Center header's headroom chip (BOSSAPP-1342): the subscription account closest to
 * its limit in a few words ("Claude Code 3% left · resets Fri 11:00"), every account behind the
 * tooltip, and a click opens Govern › Subscriptions. The numbers are Govern's own (the view's
 * `subscriptions`, read from the router's headroom), so the header, Govern and Setup agree.
 */
import React from "react";
import { Gauge } from "@phosphor-icons/react";

import { localWith } from "../../lib/local-time";
import type { HeadroomAccount, HeadroomChipView } from "../../lib/command-center-view";
import { Go } from "./hero";

const reset = (iso: string | null) => localWith(iso, { weekday: "short", hour: "numeric", minute: "2-digit" });

/** One account in words: what is left, or why nothing is. */
export function headroomWords(a: HeadroomAccount, withReset = true): string {
  if (a.login === "expired") return `${a.label}: sign-in expired`;
  if (a.state === "exhausted") return `${a.label}: limit reached${a.resetsAt ? ` · resets ${reset(a.resetsAt)}` : ""}`;
  if (a.leftPct === null) return `${a.label}: no usage yet`;
  const when = withReset && a.state !== "plenty" && a.resetsAt ? ` · resets ${reset(a.resetsAt)}` : "";
  return `${a.label} ${a.leftPct}% left${when}`;
}

/** The chip's tone, as Govern's pill: amber while it runs low, red at the limit or when its
 *  sign-in expired (work cannot use it either way). */
export function headroomTone(a: HeadroomAccount): "green" | "amber" | "red" {
  if (a.login === "expired" || a.state === "exhausted") return "red";
  return a.state === "tight" || a.state === "near_limit" ? "amber" : "green";
}

export function HeadroomChip({ view, orgId }: { view: HeadroomChipView | null | undefined; orgId?: string }) {
  const accounts = view?.accounts ?? [];
  if (!accounts.length) return null;
  const worst = accounts[0];
  const more = accounts.length - 1;
  const lines = accounts.map((a) => {
    const seat = a.pool ? ` (${a.pool}${a.takingNewWork ? ", takes new work" : ""})` : "";
    return `${headroomWords(a)}${seat}. At the limit: ${a.atLimit}`;
  });
  const tip = `${lines.join("\n")}\nOpens Govern › Subscriptions.`;
  return (
    <Go patch={{ area: "budget", tab: "subscriptions" }} orgId={orgId} className="cc-chip cc-headroom"
        testId="cc-headroom" title={tip} ariaLabel={`Subscription headroom: ${lines.join(" ")}`}>
      <span className="cc-headroom-in" data-tone={headroomTone(worst)} data-state={worst.state}>
        <Gauge size={12} weight="bold" aria-hidden />
        <span className="cc-headroom-text" data-testid="cc-headroom-text">{headroomWords(worst)}</span>
        {more > 0 ? <span className="cc-headroom-more" data-testid="cc-headroom-more">+{more}</span> : null}
      </span>
    </Go>
  );
}
