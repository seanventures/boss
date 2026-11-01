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
