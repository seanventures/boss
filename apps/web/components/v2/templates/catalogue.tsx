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

/** THE connector this build adds: the one picked, only while it is on offer here (fits the app's
 *  stack) and new to the app. A connector the app already carries is never "selected", so it
 *  never counts against one-per-build and Start never opens its sheet. */
export function effectiveConnector(picked: TemplateCard | null, cards: TemplateCard[] | null,
                                   where: { inApp: string[]; stack: string | null }): TemplateCard | null {
  if (!picked || where.inApp.includes(picked.key)) return null;
  return connectorsFrom(cards, where.stack).some((c) => c.key === picked.key) ? picked : null;
}

/** Why a card cannot be added now ("" when it can). `selected` counts only a connector new to
 *  this build: one the app already has is not a choice. */
export function connectorBlock(card: TemplateCard, selected: string | null, inApp: string[], appName = "the app"): string {
  if (inApp.includes(card.key)) return `Already in ${appName}: added in an earlier build.`;
  if (selected && !inApp.includes(selected) && selected !== card.key) {
    return "One connector per build for now. Add the next one with Add a feature once this build is done.";
  }
  return "";
}

/**
 * CONNECTORS AS CARDS, in New app and Add a feature (approved mockup, 2026-10-13): icon, name,
 * one line, "+ Add" / "Added ✓". The cards read as a multi-select, but a build takes ONE
 * connector (each brings its own security tests as a gate): once one is added the others are
 * greyed with the reason, and the next one is added with Add a feature after this build.
 * In Add a feature, a connector the app already carries shows "Already in the app", disabled.
 */
export function ConnectorCards({ cards, stack, selected, inApp = [], appName, onSelect, testId = "np-connectors", heading = "Connectors",
                                 sub = "Ready-made, secure building blocks, built into your app's own screens and style" }: {
  cards: TemplateCard[] | null;
  /** The app's stack in Add a feature (only connectors that fit it), null for a new app. */
  stack?: string | null;
  selected: string | null;
  /** Template keys the app already carries (Add a feature). */
  inApp?: string[];
  appName?: string;
  onSelect: (card: TemplateCard | null) => void;
  testId?: string;
  heading?: string;
  sub?: string;
}) {
  const shown = connectorsFrom(cards, stack);
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
          const on = selected === c.key && !there;
          const why = connectorBlock(c, selected, inApp, appName);
          const Icon = ICONS[c.key] ?? PuzzlePiece;
          return (
            <button key={c.key} type="button" className="np-conn-card" aria-pressed={on}
                    aria-disabled={why ? true : undefined} data-in-app={there || undefined}
                    data-testid={`np-connector-${c.key}`} title={why || connectorTip(c)}
                    onClick={() => { if (!why) onSelect(on ? null : c); }}>
              <span className="np-conn-top">
                <span className="np-conn-icon"><Icon size={22} aria-hidden /></span>
                <span className="np-conn-state">{there ? "Already in the app" : on ? "Added ✓" : "+ Add"}</span>
              </span>
              <b>{c.name}</b>
              <span className="np-conn-line">{there ? "Added in an earlier build." : c.summary}</span>
            </button>
          );
        })}
      </div>
      <span className="np-conn-note" data-testid={`${testId}-note`}
            title="Each connector brings its own tests, a gate of its own. Add the next one with Add a feature once this build is done.">
        One per build for now
      </span>
    </section>
  );
}
