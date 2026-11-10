/**
 * THE REVIEW PAGE'S DOCKABLE PANES (BOSSAPP-954): the tree on the left and the agent on the right
 * each DOCKED (a column), a RAIL (a thin strip of icons) or an OVERLAY (a drawer over the code,
 * opened from the rail, that never reflows the diff). Keys: [ the tree, ] the agent, \ focus mode
 * (both to rails; again to put them back). Widths and docked state are remembered per person and
 * per page type. Pure state; the page renders it (app/changes/…/review-dock.tsx).
 */

export type Pane = "tree" | "agent";
export type PaneState = "docked" | "rail" | "overlay";
export type Layout = {
  tree: PaneState; agent: PaneState; treeW: number; agentW: number;
  /** Focus mode: both panes on rails, and what they were before (restored on leaving it). */
  focus: boolean; before: { tree: PaneState; agent: PaneState } | null;
};
export type Action =
  | { type: "toggle"; pane: Pane }          // docked ↔ rail (the edge button, [ and ])
  | { type: "open"; pane: Pane }            // rail → overlay (a rail icon)
  | { type: "close"; pane: Pane }           // overlay → rail
  | { type: "pin"; pane: Pane }             // overlay/rail → docked (the pin)
  | { type: "focus"; on?: boolean }         // \ and Full screen
  | { type: "resize"; pane: Pane; width: number }
  | { type: "load"; layout: Layout };          // what this person had on this page type

export const TREE_W = { min: 160, max: 560, initial: 264 };
export const AGENT_W = { min: 260, max: 640, initial: 340 };
export const DEFAULT_LAYOUT: Layout = { tree: "docked", agent: "docked", treeW: TREE_W.initial, agentW: AGENT_W.initial,
                                        focus: false, before: null };

const clamp = (v: number, lo: number, hi: number) => Math.round(Math.max(lo, Math.min(hi, Number.isFinite(v) ? v : lo)));

export function reduce(s: Layout, a: Action): Layout {
  const other = (p: Pane): Pane => (p === "tree" ? "agent" : "tree");
  switch (a.type) {
    case "load":
      return a.layout;
    case "toggle": {
      const next: PaneState = s[a.pane] === "docked" ? "rail" : "docked";
      const out = { ...s, [a.pane]: next };
      // Docking a pane leaves focus mode (the other pane keeps its rail).
      return next === "docked" ? { ...out, focus: false, before: null } : out;
    }
    case "open":
      // One drawer at a time over the code.
      return { ...s, [a.pane]: "overlay", [other(a.pane)]: s[other(a.pane)] === "overlay" ? "rail" : s[other(a.pane)] };
    case "close":
      return s[a.pane] === "overlay" ? { ...s, [a.pane]: "rail" } : s;
    case "pin":
      return { ...s, [a.pane]: "docked", focus: false, before: null };
    case "focus": {
      const on = a.on ?? !s.focus;
      if (on === s.focus) return s;
      if (on) return { ...s, focus: true, before: { tree: s.tree === "overlay" ? "rail" : s.tree, agent: s.agent === "overlay" ? "rail" : s.agent },
                       tree: "rail", agent: "rail" };
      return { ...s, focus: false, tree: s.before?.tree ?? "docked", agent: s.before?.agent ?? "docked", before: null };
    }
    case "resize":
      return a.pane === "tree" ? { ...s, treeW: clamp(a.width, TREE_W.min, TREE_W.max) }
        : { ...s, agentW: clamp(a.width, AGENT_W.min, AGENT_W.max) };
  }
  return s;
}

/** The key shortcuts, ignored while typing in a field. */
export function keyAction(key: string, typing: boolean): Action | null {
  if (typing) return null;
  if (key === "[") return { type: "toggle", pane: "tree" };
  if (key === "]") return { type: "toggle", pane: "agent" };
  if (key === "\\") return { type: "focus" };
  return null;
}

/** What shows at this viewport width: on a tablet (<1100px) a docked agent is a drawer (a rail),
 *  on a phone (<700px) both panes are sheets opened from the header — only the code shows. */
export function effective(s: Layout, viewport: number): { tree: PaneState | "sheet"; agent: PaneState | "sheet" } {
  if (viewport < 700) return { tree: s.tree === "overlay" ? "sheet" : "rail", agent: s.agent === "overlay" ? "sheet" : "rail" };
  if (viewport < 1100) return { tree: s.tree, agent: s.agent === "docked" ? "rail" : s.agent };
  return { tree: s.tree, agent: s.agent };
}

/** The grid's column widths for the effective state (a rail is 2.25rem; an overlay floats). */
export function columns(s: Layout, viewport: number): { tree: string; agent: string } {
  const e = effective(s, viewport);
  const col = (st: string, w: number) => (st === "docked" ? `${w}px` : st === "sheet" ? "0px" : "2.25rem");
  return { tree: col(e.tree, s.treeW), agent: col(e.agent, s.agentW) };
}

export const layoutKey = (person: string, page: string) => `boss.review.layout.${person || "me"}.${page}`;

/** What was remembered, made safe (an overlay is not remembered: it comes back a rail). */
export function parseLayout(raw: string | null): Layout {
  try {
    const v = JSON.parse(raw ?? "null") as Partial<Layout> | null;
    if (!v || typeof v !== "object") return DEFAULT_LAYOUT;
    const st = (x: unknown): PaneState => (x === "rail" || x === "overlay" ? "rail" : "docked");
    const focus = v.focus === true;
    return { tree: st(v.tree), agent: st(v.agent), treeW: clamp(Number(v.treeW ?? TREE_W.initial), TREE_W.min, TREE_W.max),
             agentW: clamp(Number(v.agentW ?? AGENT_W.initial), AGENT_W.min, AGENT_W.max), focus,
             before: focus && v.before ? { tree: st(v.before.tree), agent: st(v.before.agent) } : null };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

export function loadLayout(person: string, page: string): Layout {
  try { return parseLayout(window.localStorage.getItem(layoutKey(person, page))); } catch { return DEFAULT_LAYOUT; }
}

export function saveLayout(person: string, page: string, s: Layout): void {
  try {
    const keep = { ...s, tree: s.tree === "overlay" ? "rail" : s.tree, agent: s.agent === "overlay" ? "rail" : s.agent };
    window.localStorage.setItem(layoutKey(person, page), JSON.stringify(keep));
  } catch { /* a convenience only */ }
}
