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
 * - `ConnectorChips`: "Add a connector" in New app AND Add a feature, one row, the same chips.
 */
import React, { useEffect, useMemo, useState } from "react";

import { IDEAS, type Idea } from "../../../app/ask-boss-hero";
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

/** "Add a connector": the same chips in New app and Add a feature. One per build, because each
 *  connector's security tests are their own gate: pressing another chip swaps the choice. */
export function ConnectorChips({ cards, stack, selected, onSelect, testId = "np-connectors" }: {
  cards: TemplateCard[] | null;
  /** The app's stack in Add a feature (only connectors that fit it), null for a new app. */
  stack?: string | null;
  selected: string | null;
  onSelect: (card: TemplateCard | null) => void;
  testId?: string;
}) {
  const shown = connectorsFrom(cards, stack);
  if (cards !== null && !shown.length) return null;
  return (
    <div className="np-ideas np-connectors" data-testid={testId}>
      <small title="A connector adds a capability to the app, built with its own screens and theme. One per build.">Add a connector</small>
      {cards === null ? <small className="np-dim">Loading…</small> : null}
      {shown.map((c) => {
        const on = selected === c.key;
        return (
          <button key={c.key} type="button" className="np-pill np-kind" aria-pressed={on}
                  data-testid={`np-connector-${c.key}`} title={connectorTip(c)}
                  onClick={() => onSelect(on ? null : c)}>
            {c.name}
            <span className="np-connector-stacks" aria-hidden> · {c.stacks.map(stackLabel).join("/")}</span>
          </button>
        );
      })}
    </div>
  );
}
