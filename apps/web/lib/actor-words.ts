/** Who did something, in words a person reads. Never an id.
 *
 *  "Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs", "Approved by ceo", "set by usr_…": every screen
 *  that named an actor printed the id it was given. A name the server sent wins; a word that is
 *  already a name ("BOSS", "workflow", a role) passes through; an opaque id never does. */

const ID = /^(usr|agt|svc|sa|prn)_[a-z0-9]{6,}$/i;

export function looksLikeAnId(value: string | null | undefined): boolean {
  return ID.test(String(value ?? "").trim());
}

export function actorWords(actor: string | null | undefined, name?: string | null,
                           viewer?: string | null): string {
  const id = String(actor ?? "").trim();
  if (viewer && id && id === viewer) return "you";
  const named = String(name ?? "").trim();
  if (named && !looksLikeAnId(named)) return named;
  if (!id) return "someone";
  if (id === "ceo") return "the owner";
  if (id === "auto_approval") return "automatic approval";
  if (id.startsWith("policy:")) return "a deploy rule";
  if (!looksLikeAnId(id)) return id;
  return id.toLowerCase().startsWith("agt_") ? "an agent" : "a person";
}

/** Who reviewed or approved a knowledge-graph rule. The stored name wins; a record from before
 *  names were kept holds the legacy member key ("ceo" for an Owner) and reads "the operator" —
 *  never the bare key, never "(legacy record)" (KG tester round 4), never a made-up person.
 *  The Knowledge Graph's panels use lib/kg-provenance.ts personName, which says the same. */
export function reviewerWords(by: string | null | undefined, name?: string | null,
                              viewer?: string | null): string {
  const id = String(by ?? "").trim();
  if (!String(name ?? "").trim() && id === "ceo") return "the operator";
  return actorWords(by, name, viewer);
}

/** An opaque record id inside a sentence (minv_5733948397584a8182c7, wfr_6267c5d3caa4…): a prefix
 *  and twelve or more hex or base-32 characters. */
const ID_IN_TEXT = /\s*\b[a-z]{2,6}_[0-9a-z]{12,}\b/gi;

/** A SENTENCE A PERSON READS, WITHOUT THE IDS IN IT (tester round 2: the Command Center hero read
 *  "Reconcile expired model invocation minv_5733948397584a8182c7"). The record stays linked; its id
 *  is never the words. A sentence that was only an id keeps it rather than reading empty. */
export function withoutIds(text: string | null | undefined): string {
  const raw = String(text ?? "");
  const cleaned = raw.replace(ID_IN_TEXT, (m) => (/[0-9]/.test(m) ? "" : m)).replace(/\s{2,}/g, " ").replace(/\s+([.,:;)])/g, "$1").trim();
  return cleaned || raw.trim();
}
