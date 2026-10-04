"use client";

/**
 * The server's pill, drawn. Colour is never the only signal — the label is always there — and
 * the tone class comes straight from the server (`pill.tone`); nothing here maps a state to a
 * colour.
 */
import React, { type ReactNode } from "react";
import { ArrowSquareOut, CircleNotch } from "@phosphor-icons/react";

import type { Pill as PillT, Tone } from "../../lib/setup-types";

/** Text with a tooltip. `data-tip` and `title` both carry it; the copy budget skips them. */
export function Tip({ tip, children, className, as = "span", testid }: {
  tip: string; children: ReactNode; className?: string; as?: "span" | "strong";
  testid?: string;
}) {
  const Tag = as;
  return <Tag className={className} data-tip={tip} title={tip} data-testid={testid}>{children}</Tag>;
}

export function Dot({ tone }: { tone: Tone }) {
  if (tone === "busy") {
    return <CircleNotch className="su-dot su-spin" aria-hidden="true" size={12} weight="bold" />;
  }
  return <span className={`su-dot su-tone-${tone}`} aria-hidden="true" />;
}

/** A pill's HH:MM in the viewer's own zone: the server writes its clock (UTC in a container)
 *  and sends the instant as `at`; the label and tip are rewritten here. */
export function localPill<T extends PillT>(pill: T): T {
  if (!pill.at) return pill;
  const when = new Date(pill.at);
  if (Number.isNaN(when.getTime())) return pill;
  const hhmm = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const swap = (text: string) => text.replace(/\b\d{2}:\d{2}\b/g, hhmm);
  return { ...pill, label: swap(pill.label), tip: swap(pill.tip) };
}

export function Pill({ pill: raw, testid }: { pill: PillT; testid?: string }) {
  const pill = localPill(raw);
  return (
    <span className={`su-pill su-tone-${pill.tone}`} data-tip={pill.tip} title={pill.tip}
          data-state={pill.state} data-testid={testid} role="status">
      <Dot tone={pill.tone} />
      {pill.label}
    </span>
  );
}

/** A link out of the console. `↗` is part of the label, as the copy guide spells it. */
export function Out({ href, children, tip, testid }: {
  href: string; children: ReactNode; tip?: string; testid?: string;
}) {
  return (
    <a className="su-link" href={href} target="_blank" rel="noreferrer noopener"
       title={tip} data-tip={tip} data-testid={testid}>
      {children}<ArrowSquareOut aria-hidden="true" size={12} className="su-out" />
    </a>
  );
}

/** A refusal from the API, as one pill: headline, fix as the tooltip. */
export function Refusal({ headline, fix, testid }: { headline: string; fix: string;
                                                      testid?: string }) {
  return (
    <span className="su-pill su-tone-red" data-tip={fix} title={fix} role="alert"
          data-testid={testid ?? "su-refusal"}>
      <Dot tone="red" />{headline}
    </span>
  );
}
