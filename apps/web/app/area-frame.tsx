"use client";

/**
 * AN AREA NOBODY IS LOOKING AT DOES NO WORK.
 *
 * Every area the session has opened stays mounted (lib/area-loading.ts) and the CSS reveal only
 * hides it, so every panel in every hidden area kept polling. Measured on the laptop install
 * (2026-10-10), sitting on the Command Center after opening each area once: 126 browser requests
 * a minute, ~80% of them from areas off screen (Workflows' run list, Activity's list, histogram
 * and facets, the Factory floor, Agents' live board, Infra's fleet, Govern). Several tabs
 * multiply it: the polling storm behind the API's climb and the slow pages.
 *
 * The frame tells each area's sections whether they are on screen (lib/area-shown.ts); every poll
 * asks before it fetches, and a panel coming back on screen asks once, at once. State (drafts,
 * scroll, selections) is untouched: nothing unmounts.
 *
 * A section counts as shown when the area on screen is its own OR it holds a section of that area
 * (the visibility CSS shows `section:has([data-area=X])` too; lib/area-visibility.ts). Anything
 * without a `data-area` is left alone.
 */
import React, { Children, Fragment, createContext, isValidElement, useContext,
         type ReactElement, type ReactNode } from "react";

import { AreaShownContext } from "../lib/area-shown";

/** The area on screen, provided by the console shell. Null outside it: every area counts as shown. */
export const ActiveAreaContext = createContext<string | null>(null);

type Props = Record<string, unknown> & { children?: ReactNode };

function propsOf(node: ReactNode): Props | null {
  return isValidElement(node) ? (node.props as Props) : null;
}

/** Whether `node`'s element tree (as far as it was handed over) names `area` anywhere. */
export function holdsArea(node: ReactNode, area: string, budget = { n: 2000 }): boolean {
  if (budget.n-- <= 0) return true;               // too deep to be sure: count it as shown
  if (Array.isArray(node)) return node.some((child) => holdsArea(child, area, budget));
  const props = propsOf(node);
  if (!props) return false;
  if (props["data-area"] === area) return true;
  return props.children !== undefined && holdsArea(props.children, area, budget);
}

/** The frame's children, each area section told whether it is on screen. */
export function withAreaShown(children: ReactNode, active: string | null): ReactNode {
  return Children.map(children, (child): ReactNode => {
    if (isValidElement(child) && child.type === Fragment) {
      return withAreaShown((child as ReactElement<Props>).props.children, active);
    }
    const area = propsOf(child)?.["data-area"];
    if (typeof area !== "string" || active === null) return child;
    const shown = area === active || holdsArea(child, active);
    return <AreaShownContext.Provider value={shown}>{child}</AreaShownContext.Provider>;
  });
}

export function AreaFrame({ children }: { children?: ReactNode }) {
  const active = useContext(ActiveAreaContext);
  return <div className="frame">{withAreaShown(children, active)}</div>;
}
