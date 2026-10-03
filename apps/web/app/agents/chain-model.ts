/**
 * A run's chain as the strip draws it: who handed what to whom, in the order it happened.
 *
 * Pure, so the shape the strip draws is the shape a test can hold: hops in API order, parallel
 * work stacked in one column, a step's own inner work (build stages, panel seats, a sub-run)
 * folded under it, and a repair loop said once (`Forge ↔ Rook ×3`) instead of six times.
 *
 * Nothing here writes an edge label. Every hop carries the server's `edgeLabel` from the act it
 * stands for (spec §9: edge_label() is the single source).
 */
import type { Act, ActHow, AvatarState, AvatarVariant, Chain } from "../../lib/agent-world-types";

const ACRONYMS: Record<string, string> = { qa: "QA", pm: "PM", sre: "SRE", ux: "UX", cto: "CTO", vp: "VP" };

/** A role key as a person would say it, when the resolver has nothing better. */
export function roleWords(roleKey: string): string {
  return roleKey.split(/[_\s]+/).filter(Boolean)
    .map((w) => ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
}

/** Who a hop is: an agent by role key, or a person. BOSS hands work over; it never does it. */
export type Who = { roleKey: string | null; variant: AvatarVariant; personId?: string };

export type Hop = {
  kind: "hop";
  key: string;
  /** One act normally; several when parallel work started from the same place. */
  acts: Act[];
  who: Who[];
  /** The step's own inner work (stages, seats, a sub-run), folded. */
  inside: ChainItem[];
  insideCount: number;
  /** "Build" in "+5 inside Build". */
  insideOf: string;
};
export type Loop = { kind: "loop"; key: string; a: Hop; b: Hop; rounds: number; acts: Act[] };
export type ChainItem = Hop | Loop;

/** Kinds that are part of a step's own work when they hang off one. */
const NESTED = new Set<Act["kind"]>(["stage", "seat", "run", "chat"]);

/** What an act's status looks like on a face. Only real states, never decoration. */
export function actState(act: Pick<Act, "status" | "kind">): AvatarState {
  // A run's root is the person who started it: they are not "working" while the agents do.
  if (act.kind === "run" && act.status !== "error" && act.status !== "stale") return "idle";
  switch (act.status) {
    case "active": return act.kind === "seat" ? "speaking" : "working";
    case "awaiting_input": return "needs_you";
    case "complete": return "done";
    case "error":
    case "stale": return "stuck";
    default: return "idle";
  }
}

/** Who did this act. A NULL role is a person: "You" when it is the viewer. */
export function whoDid(act: Act, viewer: string): Who {
  if (act.roleKey) return { roleKey: act.roleKey, variant: "agent" };
  const person = act.kind === "run" || act.kind === "approval"
    ? (act.handedBy.kind === "person" ? act.handedBy.id : undefined) ?? act.accountable
    : act.accountable || act.handedBy.id;
  const you = !!viewer && !!person && person === viewer;
  return { roleKey: null, variant: you ? "you" : "person", ...(person ? { personId: person } : {}) };
}

function whoKey(w: Who): string {
  return w.roleKey ?? `person:${w.personId ?? ""}`;
}

/** "Build" out of "Build · Ship it #182". */
export function shortLabel(label: string): string {
  return (label.split(" · ")[0] || label).trim();
}

/** Order acts by start, keeping the API's order for ties (and for acts with no start yet). */
function byStart(acts: Act[]): Act[] {
  return acts.map((a, i) => ({ a, i }))
    .sort((x, y) => {
      const tx = Date.parse(x.a.startedAt);
      const ty = Date.parse(y.a.startedAt);
      if (Number.isFinite(tx) && Number.isFinite(ty) && tx !== ty) return tx - ty;
      return x.i - y.i;
    })
    .map((x) => x.a);
}

function hopOf(acts: Act[], viewer: string, inside: ChainItem[] = [], insideCount = 0): Hop {
  return {
    kind: "hop", key: acts.map((a) => a.actId).join("+"), acts,
    who: acts.map((a) => whoDid(a, viewer)), inside, insideCount,
    insideOf: shortLabel(acts[0]?.label ?? ""),
  };
}

/**
 * Collapse a back-and-forth between two agents into one item: implement, review, implement,
 * review, implement, review is `Forge ↔ Rook ×3`. Two rounds at least, single hops only.
 */
export function collapseLoops(items: ChainItem[]): ChainItem[] {
  const out: ChainItem[] = [];
  const single = (it: ChainItem | undefined): it is Hop => !!it && it.kind === "hop" && it.acts.length === 1;
  const key = (it: Hop) => whoKey(it.who[0]);
  let i = 0;
  while (i < items.length) {
    const x = items[i];
    const y = items[i + 1];
    if (single(x) && single(y) && key(x) !== key(y)) {
      let j = i + 2;
      while (single(items[j]) && key(items[j] as Hop) === key((j - i) % 2 === 0 ? x : y)) j += 1;
      const run = items.slice(i, j) as Hop[];
      if (run.length >= 4) {
        out.push({ kind: "loop", key: `loop:${run[0].key}`, a: x, b: y, rounds: Math.ceil(run.length / 2),
                   acts: run.flatMap((h) => h.acts) });
        i = j;
        continue;
      }
    }
    out.push(x);
    i += 1;
  }
  return out;
}

/**
 * The strip's items for a chain. The root (`run`) act is the first hop: the person who started
 * it. Top-level work follows in start order; acts that began from the same parent stack in one
 * column; anything nested under a step folds into that step's hop.
 */
export function chainItems(chain: Pick<Chain, "acts">, viewer: string): ChainItem[] {
  const acts = chain.acts;
  if (!acts.length) return [];
  const byId = new Map(acts.map((a) => [a.actId, a]));
  const root = acts.find((a) => a.kind === "run" && (!a.parentActId || !byId.has(a.parentActId)))
    ?? acts.find((a) => !a.parentActId || !byId.has(a.parentActId))
    ?? acts[0];
  const children = new Map<string, Act[]>();
  for (const a of acts) {
    if (!a.parentActId || a === root) continue;
    const list = children.get(a.parentActId) ?? [];
    list.push(a);
    children.set(a.parentActId, list);
  }
  /** An act nested under a step: its parent is work, not the run, and it is a nested kind. */
  const nestedUnderWork = (a: Act): boolean => {
    if (!a.parentActId || a.parentActId === root.actId) return false;
    const parent = byId.get(a.parentActId);
    if (!parent) return false;
    if (parent.kind === "stage" || parent.kind === "seat") return true;
    return NESTED.has(a.kind) && parent.kind !== "run";
  };
  /** The work folded under an act: its nested children, and theirs — never the next step. */
  const nestedBelow = (id: string): Act[] => {
    const out: Act[] = [];
    const walk = (pid: string) => {
      for (const c of children.get(pid) ?? []) {
        if (!nestedUnderWork(c)) continue;
        out.push(c);
        walk(c.actId);
      }
    };
    walk(id);
    return out;
  };
  const inner = (owner: Act): { items: ChainItem[]; count: number } => {
    const all = byStart(nestedBelow(owner.actId));
    return { items: collapseLoops(all.map((a) => hopOf([a], viewer))), count: all.length };
  };

  const main = byStart(acts.filter((a) => a !== root && !nestedUnderWork(a)));
  const items: ChainItem[] = [hopOf([root], viewer)];
  for (let i = 0; i < main.length; i += 1) {
    const column = [main[i]];
    // Parallel: the next acts started from the same parent as this one.
    while (i + 1 < main.length && main[i + 1].parentActId && main[i + 1].parentActId === main[i].parentActId
           && main[i].kind === "step" && main[i + 1].kind === "step") {
      column.push(main[i + 1]);
      i += 1;
    }
    if (column.length === 1) {
      const { items: sub, count } = inner(column[0]);
      items.push(hopOf(column, viewer, sub, count));
    } else {
      items.push(hopOf(column, viewer));
    }
  }
  return [items[0], ...collapseLoops(items.slice(1))];
}

/** The act that ran this step, if any: the latest step act for it (a retry is newer). */
export function stepActOf(chain: Pick<Chain, "acts"> | null, stepId: string): Act | null {
  if (!chain) return null;
  let found: Act | null = null;
  for (const a of chain.acts) {
    if (a.kind === "step" && a.links.step_id === stepId) found = a;
  }
  return found;
}

/** The approval that answered this step's gate, if any. */
export function approvalOf(chain: Pick<Chain, "acts"> | null, stepAct: Act | null): Act | null {
  if (!chain || !stepAct) return null;
  return chain.acts.find((a) => a.kind === "approval" && a.parentActId === stepAct.actId) ?? null;
}

/** A step's agent on the lane, once the work is recorded. `how` keys the glyph. */
export type LaneAgent = {
  roleKey: string | null; variant: AvatarVariant; how: ActHow | string; edgeLabel: string;
  state: AvatarState; considered: number;
};

/** What the runtime recorded about who ran a step (`output.ran_as`). */
type RanAs = { agent?: string; how?: string };

/** ran_as.how in the act vocabulary, for a step whose act has not reached the console. */
const RAN_AS_HOW: Record<string, ActHow> = {
  named: "named", model: "model_pick", word_overlap: "model_pick", only_holder: "only_holder",
  only_candidate: "only_candidate", run_actor: "run_actor",
};

/**
 * Who ran a step, for its lane node: from its act when there is one, otherwise from `ran_as`.
 * Nothing before either exists — so an Auto step stays Auto until BOSS has actually picked.
 */
export function laneAgentOf(output: unknown, act: Act | null, viewer: string): LaneAgent | undefined {
  const ran = (output && typeof output === "object" ? (output as { ran_as?: RanAs }).ran_as : undefined);
  if (act && act.kind === "step") {
    const who = whoDid(act, viewer);
    return { roleKey: who.roleKey, variant: who.variant, how: act.how, edgeLabel: act.edgeLabel,
             state: actState(act), considered: act.considered.length };
  }
  if (!ran || !ran.agent) return undefined;
  const how = RAN_AS_HOW[ran.how ?? ""] ?? ran.how ?? "";
  const person = how === "run_actor";
  return { roleKey: person ? null : ran.agent, variant: person ? "you" : "agent", how, edgeLabel: "",
           state: "idle", considered: 0 };
}

/** Distinct agents that did work in this chain (people are not counted as agents). */
export function agentsIn(chain: Pick<Chain, "credits" | "acts">): string[] {
  const keys = chain.credits.length
    ? chain.credits.map((c) => c.roleKey)
    : chain.acts.map((a) => a.roleKey);
  return [...new Set(keys.filter((k): k is string => !!k))];
}

/** The minutes a chain took, as a short duration. */
export function minutesLabel(minutes: number): string {
  if (!minutes || minutes < 1) return "<1m";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes - h * 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

export function costLabel(cost: number): string {
  return cost >= 10 ? `$${cost.toFixed(0)}` : `$${cost.toFixed(2)}`;
}
