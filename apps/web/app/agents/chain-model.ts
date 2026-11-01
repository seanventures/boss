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
import { usd } from "../../lib/money";
import type { Act, ActHow, AvatarState, AvatarVariant, Chain } from "../../lib/agent-world-types";
import { BOSS_RAN_IT, HUMAN, NOT_AN_AGENT } from "../../lib/ran-as";

const ACRONYMS: Record<string, string> = { qa: "QA", pm: "PM", sre: "SRE", ux: "UX", cto: "CTO", vp: "VP" };

/** A role key as a person would say it, when the resolver has nothing better. */
export function roleWords(roleKey: string): string {
  return roleKey.split(/[_\s]+/).filter(Boolean)
    .map((w) => ACRONYMS[w.toLowerCase()] ?? (w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
}

/** Who a hop is: an agent by role key, a person (by name when BOSS knows it), or BOSS. */
export type Who = { roleKey: string | null; variant: AvatarVariant; personId?: string; name?: string };

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
  /** A retried step: every attempt, oldest first, each with its own inner work. The hop itself
   *  is the latest; the chain says the step once, with the count (`Forge · 7 attempts`). */
  attempts?: Hop[];
  /** The same actor's consecutive steps, said once (`Seed Owner · 3 steps`), oldest first. */
  steps?: string[];
};
export type Loop = { kind: "loop"; key: string; a: Hop; b: Hop; rounds: number; acts: Act[] };
export type ChainItem = Hop | Loop;

/** Kinds that are part of a step's own work when they hang off one. */
const NESTED = new Set<Act["kind"]>(["stage", "seat", "run", "chat"]);

/** What an act's status looks like on a face. Only real states, never decoration.
 *
 *  HONEST ENDINGS (operator, 2026-10-13): an act that errored FAILED; an act closed `stale` was
 *  left open because its run or build stopped, so it STOPPED. Neither is "stuck": the strip
 *  said "Stuck · 29s" on a Check that had failed and "Stuck · 3m" on a Review that never ran
 *  because the build was blocked. An act's record cannot say it went quiet, so nothing here
 *  says stuck: that is the server's call on a live build (factory_view QUIET_AFTER_MINUTES). */
export function actState(act: Pick<Act, "status" | "kind">): AvatarState {
  // A run's root is the person who started it: they are not "working" while the agents do.
  if (act.kind === "run" && act.status !== "error" && act.status !== "stale") return "idle";
  switch (act.status) {
    case "active": return act.kind === "seat" ? "speaking" : "working";
    case "awaiting_input": return "needs_you";
    case "complete": return "done";
    case "error": return "failed";
    case "stale": return "stopped";
    default: return "idle";
  }
}

/**
 * Who did this act. The server's resolved actor first (agent_acts.resolve_actors: a build step's
 * builder, a panel's chair, a gate's approver, or BOSS), so a built-in step that ran BOSS's
 * tool is never drawn as the person who started the run. A person is "You" only when they are
 * the viewer, by name when BOSS knows it, and "A person" only when nothing names them.
 */
export function whoDid(act: Act, viewer: string): Who {
  if (act.roleKey) return { roleKey: act.roleKey, variant: "agent" };
  const a = act.actor;
  if (a?.kind === "agent" && a.roleKey) return { roleKey: a.roleKey, variant: "agent" };
  if (a?.kind === "boss") return { roleKey: null, variant: "boss" };
  if (a?.kind === "person") {
    const you = !!viewer && !!a.personId && a.personId === viewer;
    return { roleKey: null, variant: you ? "you" : "person",
             ...(a.personId ? { personId: a.personId } : {}), ...(a.name ? { name: a.name } : {}) };
  }
  const person = act.kind === "run" || act.kind === "approval"
    ? (act.handedBy.kind === "person" ? act.handedBy.id : undefined) ?? act.accountable
    : act.accountable || act.handedBy.id;
  const you = !!viewer && !!person && person === viewer;
  return { roleKey: null, variant: you ? "you" : "person", ...(person ? { personId: person } : {}) };
}

function whoKey(w: Who): string {
  return w.roleKey ?? (w.variant === "boss" ? "boss" : `person:${w.personId ?? ""}`);
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

/** What work a hop did: its step, or for a stage/seat the step it is part of, else its label. */
function workOf(h: Hop): string {
  const a = h.acts[0];
  return a.links.step_id ?? shortLabel(a.label);
}

/**
 * Collapse a back-and-forth between two agents into one item: implement, review, implement,
 * review, implement, review is `Forge ↔ Rook ×3`. Two rounds at least, single hops only.
 *
 * A LOOP IS THE SAME WORK COMING BACK, not two names taking turns. Built-in steps run as You, so
 * research (You) → PRD (PM) → readiness (You) → approve (PM) → deploy (You) alternates between
 * two faces while doing five different things; collapsing that hid five steps behind
 * `You ↔ PM ×3`. Each hop must repeat both the agent and the work of the hop two before it.
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
      while (single(items[j]) && key(items[j] as Hop) === key(items[j - 2] as Hop)
             && workOf(items[j] as Hop) === workOf(items[j - 2] as Hop)) j += 1;
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

  const top = byStart(acts.filter((a) => a !== root && !nestedUnderWork(a)));
  // ONE STEP, ONE HOP. A retry — by a person or BOSS's own — is a new act for the same step,
  // and every attempt shares the parent the step came from, so they stacked as "parallel"
  // work: seven identical faces for one build. The step is said once, by its latest attempt,
  // with the attempts folded under it.
  const attemptsOf = new Map<string, Act[]>();
  for (const a of top) {
    if (a.kind !== "step" || !a.links.step_id) continue;
    const list = attemptsOf.get(a.links.step_id) ?? [];
    list.push(a);
    attemptsOf.set(a.links.step_id, list);
  }
  const latest = (a: Act): boolean => {
    const list = a.kind === "step" && a.links.step_id ? attemptsOf.get(a.links.step_id) : undefined;
    return !list || list[list.length - 1] === a;
  };
  const main = top.filter(latest);
  const withAttempts = (hop: Hop): Hop => {
    const a = hop.acts[0];
    const list = hop.acts.length === 1 && a.kind === "step" && a.links.step_id ? attemptsOf.get(a.links.step_id) : undefined;
    if (!list || list.length < 2) return hop;
    return { ...hop, attempts: list.map((x) => { const { items: sub, count } = inner(x); return hopOf([x], viewer, sub, count); }) };
  };
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
      items.push(withAttempts(hopOf(column, viewer, sub, count)));
    } else {
      items.push(hopOf(column, viewer));
    }
  }
  return mergeRepeats([items[0], ...collapseLoops(items.slice(1))]);
}

/** What a hop did, in a step list: its step's label, or its own. */
function hopWork(h: Hop): string {
  const a = h.acts[0];
  return shortLabel(a?.label ?? "") || a?.links.step_id || "";
}

/**
 * ONE ACTOR, ONE STOP. "Seed Owner → Seed Owner → Juno → Seed Owner → Seed Owner" said the same
 * person five times and ran the strip off the card. Consecutive hops by the same actor are one
 * stop that names the steps it covers (`· 3 steps`, listed on hover); their inner work folds
 * together. A retried step keeps its own stop: its attempts open from it.
 */
export function mergeRepeats(items: ChainItem[]): ChainItem[] {
  const out: ChainItem[] = [];
  // A retried step merges too: "Seed Owner · N steps → Seed Owner · N attempts" put the same
  // person twice side by side. Its attempts travel with the merged stop.
  const single = (it: ChainItem | undefined): it is Hop =>
    !!it && it.kind === "hop" && it.acts.length === 1;
  const work = (h: Hop) => {
    const n = h.attempts?.length ?? 0;
    const w = hopWork(h);
    return w && n > 1 ? `${w} ×${n}` : w;
  };
  for (const it of items) {
    const prev = out[out.length - 1];
    if (single(it) && single(prev) && whoKey(prev.who[0]) === whoKey(it.who[0])) {
      // DISTINCT steps, not records: a step written many times is still one step.
      const steps = [...new Set([...(prev.steps ?? [work(prev)]), work(it)].filter(Boolean))];
      const attempts = [...(prev.attempts ?? []), ...(it.attempts ?? [])];
      out[out.length - 1] = {
        ...prev, key: `${prev.key}~${it.key}`, steps,
        // The latest act stands for the stop: its face says how it is now.
        acts: it.acts, who: it.who,
        ...(attempts.length > 1 ? { attempts } : { attempts: undefined }),
        inside: [...prev.inside, ...it.inside], insideCount: prev.insideCount + it.insideCount,
        insideOf: prev.insideCount && it.insideCount ? "these steps" : prev.insideCount ? prev.insideOf : it.insideOf,
      };
      continue;
    }
    out.push(it);
  }
  return out;
}

/** The distinct actors a strip shows (agents, people and BOSS): what its count says. It counts
 *  everyone a person can reach from the strip, folded ones included ("+5 inside Meeting",
 *  "+3 more"): the header said "4 taking part" over a strip naming more than four. */
export function actorsShown(items: ChainItem[]): string[] {
  const keys = new Set<string>();
  const visit = (list: ChainItem[]) => {
    for (const it of list) {
      const hops = it.kind === "hop" ? [it] : [it.a, it.b];
      for (const h of hops) {
        for (const w of h.who) keys.add(whoKey(w));
        visit(h.inside);
      }
    }
  };
  visit(items);
  return [...keys];
}

/** A long strip keeps its first stops and its last ones, and folds the middle into `+N`. */
export const CHAIN_HEAD = 2;
export const CHAIN_TAIL = 3;
export function foldMiddle<T>(items: T[]): { head: T[]; middle: T[]; tail: T[] } {
  if (items.length <= CHAIN_HEAD + CHAIN_TAIL + 1) return { head: items, middle: [], tail: [] };
  return { head: items.slice(0, CHAIN_HEAD), middle: items.slice(CHAIN_HEAD, items.length - CHAIN_TAIL),
           tail: items.slice(items.length - CHAIN_TAIL) };
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
  /** A person: who, and their name when BOSS knows it. */
  personId?: string; name?: string;
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
 *
 * `ran_as` names more than agents (lib/ran-as.ts): the run's actor, a person answering a gate
 * (`human`), the workflow itself or the decision model. None of those gets an agent's face —
 * a person is You only when it is the viewer, and BOSS's own machinery is BOSS.
 */
export function laneAgentOf(output: unknown, act: Act | null, viewer: string): LaneAgent | undefined {
  const ran = (output && typeof output === "object" ? (output as { ran_as?: RanAs }).ran_as : undefined);
  if (act && act.kind === "step") {
    const who = whoDid(act, viewer);
    return { roleKey: who.roleKey, variant: who.variant, how: act.how, edgeLabel: act.edgeLabel,
             state: actState(act), considered: act.considered.length,
             ...(who.personId ? { personId: who.personId } : {}), ...(who.name ? { name: who.name } : {}) };
  }
  // A GATE NOBODY HAD TO ANSWER. An automatic approval was drawn as "A person" on the lane
  // (the gate's planned actor) while BOSS approved it; it is BOSS's.
  if (!ran && (output as { approved_by?: unknown } | null)?.approved_by === "auto_approval") {
    return { roleKey: null, variant: "boss", how: "", edgeLabel: "", state: "idle", considered: 0 };
  }
  if (!ran || !ran.agent) return undefined;
  const how = RAN_AS_HOW[ran.how ?? ""] ?? ran.how ?? "";
  const rawHow = ran.how ?? "";
  if (NOT_AN_AGENT.includes(rawHow) || ran.agent === HUMAN) {
    const variant: AvatarVariant = BOSS_RAN_IT.includes(rawHow) ? "boss"
      : !!viewer && ran.agent === viewer ? "you" : "person";
    return { roleKey: null, variant, how, edgeLabel: "", state: "idle", considered: 0 };
  }
  // A bank's own agent carries its display name (its key is `ext_…`, not a name).
  const named = (ran as { name?: unknown }).name;
  return { roleKey: ran.agent, variant: "agent", how, edgeLabel: "", state: "idle", considered: 0,
           ...(typeof named === "string" && named ? { name: named } : {}) };
}

/**
 * Who a credit names. An agent by role key; a person is You only when that person is the viewer
 * — the one the credit names (`personId`), else the run's accountable person. The Credits
 * dialog, its copied text and the PNG all name people through this; the PNG passes no viewer,
 * because an image is for other people and nobody on it is "You".
 */
export function creditWho(credit: Chain["credits"][number], accountable: string, viewer: string,
                          people: Chain["people"] = {}): Who {
  if (credit.roleKey) return { roleKey: credit.roleKey, variant: "agent" };
  const person = credit.personId || accountable || undefined;
  const you = !!viewer && !!person && person === viewer;
  const name = person ? people?.[person] : undefined;
  return { roleKey: null, variant: you ? "you" : "person", ...(person ? { personId: person } : {}),
           ...(name ? { name } : {}) };
}

/** A person as a credit names them: You, their name, or "A person" when nothing names them. */
export function personName(who: Who): string {
  return who.variant === "you" ? "You" : who.variant === "boss" ? "BOSS" : who.name || "A person";
}

/** Distinct agents behind a chain's acts, by who did each (a build step is its builder). */
export function agentKeys(chain: Pick<Chain, "acts">): string[] {
  return [...new Set(chain.acts.map((a) => whoDid(a, "").roleKey).filter((k): k is string => !!k))];
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

/** Money on the run surfaces: lib/money.ts `usd`, the one format. */
export function costLabel(cost: number): string {
  return usd(cost);
}

/**
 * What work cost, said truthfully: money, and what a subscription covered beside it. A build
 * on a Claude Code subscription spends no money and ~$30 of list-price work per attempt; "$0.00"
 * alone said nothing happened (included_spend.py keeps the two apart, and so does this).
 */
export function spendLabel(cost: number, included = 0): string {
  if (included > 0 && cost > 0) return `${usd(cost)} + ${usd(included)} included`;
  if (included > 0) return `${usd(included)} included`;
  return usd(cost);
}

/** The tooltip that says what "included" means wherever spendLabel says it. */
export const INCLUDED_TIP = "Included: list-price value of work a subscription covered. Not money, not counted against budgets.";
