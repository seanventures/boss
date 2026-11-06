/**
 * AN ANSWER'S CITATIONS, BY NAME (rulebook source G6). An agent cites what it relied on as
 * `[R:kgn_…]` (a rule, a data-quality rule too), `[D:kgn_…]` (a document's clause or another node a
 * rulebook put in the graph) or `[L:kgn_…]` (a learning). The console never shows those ids: each
 * becomes a link to the cited item in the Knowledge Graph, named by its title.
 */

export const CITATION_RE = /\[(R|D|L):(kgn_[A-Za-z0-9_]+)\]/g;

export type CitedName = { title: string; kind: string; kind_label: string };

/** The node ids a text cites, in order, once each. */
export function citedIdsIn(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(CITATION_RE)) if (!out.includes(m[2])) out.push(m[2]);
  return out;
}

/** Markdown with every citation a link to its item, named; one not (yet) named reads
 *  "a cited item" — never its id. */
export function withCitationLinks(text: string, names: Record<string, CitedName>, hrefOf: (id: string) => string): string {
  return text.replace(CITATION_RE, (_all, _tag: string, id: string) => {
    const title = (names[id]?.title ?? "a cited item").replace(/[[\]]/g, "");
    return `[${title}](${hrefOf(id)})`;
  });
}

/**
 * A LEDGER CITATION (autonomous agents, design §11.6; stream 907). Ask BOSS answers "what did you
 * do and why?" from the decision ledger and cites each row as `[act:<act id>]`. The console
 * never shows the id: each becomes a link to that decision on its work item's flow chart in
 * Agents at work (`?area=agent_ops&act=…` opens the item on that decision's drawer).
 */
export const ACT_CITATION_RE = /\[act:(act_[A-Za-z0-9_-]+)\]/g;

export function citedActsIn(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(ACT_CITATION_RE)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

export function actFlowHref(actId: string, search = ""): string {
  const now = new URLSearchParams(search);
  const out = new URLSearchParams();
  for (const k of ["org", "project"]) { const v = now.get(k); if (v) out.set(k, v); }
  out.set("area", "agent_ops");
  out.set("act", actId);
  return `/?${out.toString()}`;
}

/** Markdown with every `[act:…]` a link named "see the flow", numbered when there are several. */
export function withActLinks(text: string, search = ""): string {
  const acts = citedActsIn(text);
  return text.replace(ACT_CITATION_RE, (_all, id: string) => {
    const n = acts.length > 1 ? ` ${acts.indexOf(id) + 1}` : "";
    return `[see the flow${n} ↗](${actFlowHref(id, search)})`;
  });
}
