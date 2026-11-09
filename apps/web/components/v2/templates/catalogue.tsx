"use client";
/**
 * CONNECTORS AND IDEAS: the catalogue as people use it (operator, 2026-10-13).
 *
 * "These are not templates, these are connectors: login, payments, checkout can be added to any
 * application … customized to their application ui … Hello endpoint, product catalog, banking
 * starter can be examples … display as Ideas in the New project section too."
 *
 * - `useIdeas`: THE ideas source. The catalogue's ideas (apps with a reference implementation)
 *   first, then the written briefs (IDEAS). The composer's "Start from an idea" and the Command
 *   Center's Ideas shelf both read it, so the two lists can never drift apart.
 * - `ConnectorCards`: the connectors in New app AND Add a feature, the same cards.
 */
import React, { useEffect, useMemo, useState } from "react";

import { IDEAS, type Idea } from "../../../app/ask-boss-hero";
import { CreditCard, EnvelopeSimple, LockKey, PaypalLogo, PuzzlePiece } from "@phosphor-icons/react";

import { stackLabel, templatesApi, type TemplateCard } from "../../../lib/templates";

/** An idea as both lists show it; `card` when it has a reference implementation to build from. */
export type IdeaItem = Idea & { card?: TemplateCard };

let cached: Promise<TemplateCard[]> | null = null;

/** The approved catalogue, read once per page (both rows and the shelf share it). */
export function useCatalogue(): TemplateCard[] | null {
  const [cards, setCards] = useState<TemplateCard[] | null>(null);
  useEffect(() => {
    let alive = true;
    cached ??= templatesApi.list().catch(() => { cached = null; return [] as TemplateCard[]; });
    void cached.then((all) => { if (alive) setCards(all.filter((c) => c.status === "active")); });
    return () => { alive = false; };
  }, []);
  return cards;
}

/** For tests: forget the page's catalogue read. */
export function resetCatalogue(): void { cached = null; }

const GLYPH_BY_KEY: Record<string, Idea["glyph"]> = {
  "banking-starter": "shield", "catalog-cart": "tag", hello: "spark",
};

/** A catalogue idea written as a brief: what it is and what it must do, in the IDEAS shape. */
export function ideaFromCard(card: TemplateCard): IdeaItem {
  const must = card.adds.length ? `\nWhat it must do:\n${card.adds.map((a) => `- ${a}`).join("\n")}` : "";
  return {
    tag: "Starter", title: card.name, glyph: GLYPH_BY_KEY[card.key] ?? "spark", card,
    brief: `Build ${card.name}.\n${card.summary}.${must}`,
  };
}

/** THE ideas list: the catalogue's starters, then the written briefs. Training is never one. */
export function ideasFrom(cards: TemplateCard[] | null): IdeaItem[] {
  const starters = (cards ?? []).filter((c) => c.kind === "idea").map(ideaFromCard);
  return [...starters, ...IDEAS];
}

export function useIdeas(): IdeaItem[] {
  const cards = useCatalogue();
  return useMemo(() => ideasFrom(cards), [cards]);
}

export function connectorsFrom(cards: TemplateCard[] | null, stack?: string | null): TemplateCard[] {
  return (cards ?? []).filter((c) => c.kind === "connector" && (!stack || c.stacks.includes(stack)));
}

/** A connector chip's one line: what it adds, what it needs, where it runs. */
export function connectorTip(card: TemplateCard): string {
  const needs = card.secrets.length ? ` Needs: ${card.secrets.map((s) => s.name).join(", ")} (test values for build and preview).` : "";
  const talks = card.egress.length ? ` Talks to: ${card.egress.join(", ")}.` : "";
  return `${card.summary}. Built in your app's own look.${needs}${talks} Stacks: ${card.stacks.map(stackLabel).join(", ")}.`;
}

/** A connector's mark on its card. */
const ICONS: Record<string, React.ComponentType<{ size?: number; "aria-hidden"?: boolean }>> = {
  "login-oauth": LockKey, "stripe-checkout": CreditCard, "paypal-checkout": PaypalLogo,
  "transactional-email": EnvelopeSimple,
};

/** The built-in connectors with a mark of their own (an imported one shows the puzzle piece). */
export const MARKED_CONNECTORS: readonly string[] = Object.keys(ICONS);

/** Most connectors one build takes (POST /v1/templates/compose takes 2 to 6; one goes alone). */
export const MAX_CONNECTORS = 6;

/** THE connectors this build adds, in the order picked: only those on offer here (fit the app's
 *  stack) and new to the app. A connector the app already carries is never "selected", so it
 *  never counts against the limit and Start never opens its sheet. One left: the single
 *  connector's sheet and POST /v1/templates/{id}/apply, as before; two or more: one build
 *  through POST /v1/templates/compose (BOSSAPP-526). */
export function effectiveConnectors(picked: TemplateCard[], cards: TemplateCard[] | null,
                                    where: { inApp: string[]; stack: string | null }): TemplateCard[] {
  const offered = new Set(connectorsFrom(cards, where.stack).map((c) => c.key));
  return picked.filter((p) => offered.has(p.key) && !where.inApp.includes(p.key));
}

/** The stacks every one of `cards` supports, in the first card's order ([] when none is shared). */
export function sharedStacks(cards: Pick<TemplateCard, "stacks">[]): string[] {
  if (!cards.length) return [];
  return cards[0].stacks.filter((s) => cards.every((c) => c.stacks.includes(s)));
}

/** Why a card cannot be added now ("" when it can), as far as the console can know before the
 *  server checks the whole set: already in the app, the limit reached, or no stack shared with
 *  the ones already added. Conflicts the server finds (two connectors serving one route, owning
 *  one file) are listed by the sheet, before and after Start. */
export function connectorBlock(card: TemplateCard, selected: TemplateCard[], inApp: string[], appName = "the app"): string {
  if (inApp.includes(card.key)) return `Already in ${appName}: added in an earlier build.`;
  if (selected.some((s) => s.key === card.key)) return "";
  if (selected.length >= MAX_CONNECTORS) {
    return `Up to ${MAX_CONNECTORS} connectors in one build. Remove one, or add this one with Add a feature after this build.`;
  }
  if (selected.length && !sharedStacks([...selected, card]).length) {
    return `No stack fits ${card.name} and ${selected.map((s) => s.name).join(", ")} together. Add it in a build of its own.`;
  }
  return "";
}

/**
 * CONNECTORS AS CARDS, in New app and Add a feature (approved mockup, 2026-10-13): icon, name,
 * one line, "+ Add" / "Added ✓". A MULTI-SELECT (BOSSAPP-526): one connector goes through its own
 * sheet, two to six through one build (the compose sheet). A card the server would refuse is
 * greyed with the reason (already in the app, the limit, no shared stack); conflicts between
 * the ones added are the sheet's to list. Each card is a toggle button (aria-pressed), so Tab
 * and Space/Enter pick and unpick; its state says its place in the build ("Added ✓, 2 of 3")
 * and the live note says how many are added.
 */
export function ConnectorCards({ cards, stack, selected, inApp = [], appName, onSelect, testId = "np-connectors", heading = "Connectors",
                                 sub = "Ready-made, secure building blocks, built into your app's own screens and style" }: {
  cards: TemplateCard[] | null;
  /** The app's stack in Add a feature (only connectors that fit it), null for a new app. */
  stack?: string | null;
  /** The connectors added so far, in the order added. */
  selected: TemplateCard[];
  /** Template keys the app already carries (Add a feature). */
  inApp?: string[];
  appName?: string;
  /** The new selection, in the order added. */
  onSelect: (cards: TemplateCard[]) => void;
  testId?: string;
  heading?: string;
  sub?: string;
}) {
  const shown = connectorsFrom(cards, stack);
  const chosen = selected.filter((s) => !inApp.includes(s.key));
  if (cards !== null && !shown.length) return null;
  return (
    <section className="np-conn" aria-labelledby={`${testId}-h`} data-testid={testId}>
      <div className="np-conn-head">
        <h3 id={`${testId}-h`}>{heading}</h3>
        <span className="np-dim">{sub}</span>
      </div>
      {cards === null ? <span className="np-dim">Loading…</span> : null}
      <div className="np-conn-grid">
        {shown.map((c) => {
          const there = inApp.includes(c.key);
          const on = !there && selected.some((s) => s.key === c.key);
          const why = connectorBlock(c, selected, inApp, appName);
          const at = on ? chosen.findIndex((s) => s.key === c.key) + 1 : 0;
          const Icon = ICONS[c.key] ?? PuzzlePiece;
          return (
            <button key={c.key} type="button" className="np-conn-card" aria-pressed={on}
                    aria-disabled={why ? true : undefined} data-in-app={there || undefined}
                    data-testid={`np-connector-${c.key}`} title={why || connectorTip(c)}
                    onClick={() => { if (!why) onSelect(on ? selected.filter((s) => s.key !== c.key) : [...selected, c]); }}>
              <span className="np-conn-top">
                <span className="np-conn-icon"><Icon size={22} aria-hidden /></span>
                <span className="np-conn-state">{there ? "Already in the app" : on ? (chosen.length > 1 ? `Added ✓, ${at} of ${chosen.length}` : "Added ✓") : "+ Add"}</span>
              </span>
              <b>{c.name}</b>
              <span className="np-conn-line">{there ? "Added in an earlier build." : c.summary}</span>
            </button>
          );
        })}
      </div>
      <span className="np-conn-note" data-testid={`${testId}-note`} role="status" aria-live="polite"
            title={`Pick up to ${MAX_CONNECTORS}. Two or more are added in one build, wired in the order picked; a connector another needs goes first. Each keeps its own tests as a gate.`}>
        {chosen.length > 1 ? `${chosen.length} added, one build` : `Up to ${MAX_CONNECTORS} in one build`}
      </span>
    </section>
  );
}
