/**
 * PROVENANCE IN WORDS, ONE FORMATTER (KG tester round 4). The node panel, the rule panel and the
 * learning panel each said where an item came from in their own way, and each leaked a record:
 * "Indexed by the code index", "Run wfr_6267c5d3… ↗ · step scan", "scan id rscan_caa44184979a_scan",
 * "content key d376f6deefbc3548", "Approved by an owner (legacy record)", an "Approved by" node
 * named "ceo", "Written by an agent (not recorded)". Every one of those screens now asks here.
 *
 * The rules: an agent by its name, its role and its Passport (a link, its id in the tooltip —
 * never in the words); a person by their display name, else "the operator"; a run or a
 * conversation by its title; code as "Indexed from <repo>@<short sha> on <date>". No raw id
 * (RAW_ID_PATTERNS, enforced by tests/kg-provenance-in-words.test.tsx) ever reaches the words.
 */
import type { KgLineageUse, KgProvenance, KgProvenanceEvent } from "@boss/api-client";

import { roleWords } from "../app/agents/chain-model";
import { actorWords, looksLikeAnId } from "./actor-words";
import { kgRunHref, kgWhen } from "./kg-view";

/** What must never be read in provenance. A short commit sha after "@" is words; these are not. */
export const RAW_ID_PATTERNS: Array<{ name: string; re: RegExp }> = [
  { name: "a prefixed record id (wfr_, rscan_, agt_, usr_, amsg_, kgn_…)", re: /\b[a-z]{2,6}_(?=[0-9a-z]*\d)[0-9a-z]{6,}/i },
  { name: "a principal or record id", re: /\b(usr|agt|svc|sa|prn|wfr|ctr|athr|amsg|kgn|kge|rscan|brec|minv)_\w+/i },
  { name: "a content key or long hash", re: /\b[0-9a-f]{12,}\b/i },
  { name: "the legacy owner key", re: /\bceo\b/i },
  { name: "a legacy-record label", re: /legacy record/i },
  { name: "the indexer's internal name", re: /code index\b/i },
  { name: "a not-recorded placeholder", re: /not recorded/i },
  { name: "an actor kind standing for a name", re: /\bby (person|agent|system)\b/i },
];

/** The raw ids and placeholders in a piece of rendered provenance (empty when it is all words). */
export function rawIdsIn(text: string): string[] {
  return RAW_ID_PATTERNS.filter((p) => p.re.test(text)).map((p) => `${p.name}: ${text.match(p.re)?.[0]}`);
}

/** A piece of a provenance line: words, a link, or an agent's Passport. */
export type ProvPart =
  | { kind: "text"; text: string; strong?: boolean; testId?: string }
  | { kind: "link"; text: string; href: string; testId?: string }
  | { kind: "passport"; roleKey: string; agentId: string | null; name: string };
export type ProvLine = ProvPart[];

const OPERATOR = "the operator";
const GENERAL = "BOSS, the general agent";

type AgentRef = { agent_name?: string | null; agent_role?: string | null };

/** An agent by its name and role: "Site Reliability Engineer (SRE)", "Atlas (Chief Architect)";
 *  BOSS itself (no agent addressed) is "BOSS, the general agent". Null when neither is known. */
export function agentName(p: AgentRef): string | null {
  const name = String(p.agent_name ?? "").trim();
  const role = String(p.agent_role ?? "").trim();
  if (role === "boss" || /^BOSS \(general\)$/i.test(name)) return GENERAL;
  const roleW = role && !looksLikeAnId(role) ? roleWords(role) : "";
  if (name && !looksLikeAnId(name)) {
    return roleW && roleW.toLowerCase() !== name.toLowerCase() ? `${name} (${roleW})` : name;
  }
  return roleW || null;
}

/** A person by their display name; a legacy key ("ceo"), an id or a bare kind is "the operator". */
export function personName(by: string | null | undefined, name?: string | null): string {
  const named = String(name ?? "").trim();
  if (named && !looksLikeAnId(named) && named !== "ceo") return named;
  const id = String(by ?? "").trim();
  if (!id || id === "ceo" || looksLikeAnId(id) || /^(usr|prn|sa|svc)_/i.test(id) || id === "person" || id === "agent" || id === "system") return OPERATOR;
  const words = actorWords(id, null);
  return rawIdsIn(words).length ? OPERATOR : words;
}

/** Who did a timeline event: a person by name, an agent's words as the server said them. */
function eventBy(e: KgProvenanceEvent): string | null {
  if (!e.by && !e.by_name) return null;
  if (e.by_name) return personName(e.by, e.by_name);
  const by = String(e.by ?? "");
  if (by === "ceo" || looksLikeAnId(by) || /^(usr|prn|sa|svc)_/i.test(by) || by === "person") return OPERATOR;
  if (/^[a-z]+(_[a-z]+)+$/.test(by)) return roleWords(by);     // a role key: "chief_architect"
  if (/^BOSS \(general\)$/i.test(by)) return GENERAL;
  return rawIdsIn(by).length ? OPERATOR : by;
}

/** "approved by Ada Lovelace" — one timeline row's words. */
export function eventWords(e: KgProvenanceEvent): string {
  const by = eventBy(e);
  return `${e.what}${by ? ` by ${by}` : ""}`;
}

/** The step a run was at, in words ("chat" is a conversation, said by its own line). */
export function stepWords(step: string | null | undefined): string | null {
  const s = String(step ?? "").trim();
  if (!s || s === "chat" || looksLikeAnId(s)) return null;
  return `its “${s.replace(/[-_]+/g, " ")}” step`;
}

function words(text: string, extra: Partial<{ strong: boolean; testId: string }> = {}): ProvPart {
  return { kind: "text", text, ...extra };
}

/** Who wrote it, as the first line's subject (no verb). */
function writer(p: KgProvenance, layer: string): ProvPart[] {
  if (layer === "code") return [];
  if (p.created_by_kind === "person") return [words(personName(p.created_by, null), { strong: true, testId: "kg-panel-who" })];
  const name = agentName(p);
  if (name) {
    const parts: ProvPart[] = [words(name, { strong: true, testId: "kg-panel-who" })];
    if (p.agent_role && p.agent_role !== "boss") parts.push(words(" · "), { kind: "passport", roleKey: p.agent_role, agentId: p.agent_id ?? null, name });
    return parts;
  }
  const who = String(p.who ?? "").trim();
  return [words(who && !rawIdsIn(who).length ? who : "an agent, before BOSS kept agents' names", { strong: true, testId: "kg-panel-who" })];
}

/** Every line of an item's provenance, in words: who, where from, the model, the approval, the
 *  version. `layer` is the item's (code | rules | learning | domain). */
export function provenanceLines(p: KgProvenance, { layer, orgId }: { layer: string; orgId?: string | null }): ProvLine[] {
  const lines: ProvLine[] = [];
  const at = p.at ? kgWhen(p.at) : "";
  if (layer === "code") {
    const src = p.source;
    const where = src?.repo ? `${src.repo}${src.sha ? `@${src.sha}` : ""}` : "the repository";
    const on = src?.at ? kgWhen(src.at) : at;
    lines.push([words("Indexed from "), words(where, { strong: true, testId: "kg-panel-who" }), ...(on ? [words(` on ${on}`)] : [])]);
    return lines;
  }
  const verb = p.created_by_kind === "person" ? "Added by " : layer === "rules" ? "Proposed by " : "Written by ";
  lines.push([words(verb), ...writer(p, layer), ...(at ? [words(` · ${at}`)] : [])]);

  const href = kgRunHref(p, orgId);
  const step = stepWords(p.step);
  if (p.run_title || href || p.run_id) {
    const title = p.run_title ? `“${p.run_title}”` : "its run";
    const line: ProvLine = [words("In ")];
    line.push(href ? { kind: "link", text: `${title} ↗`, href, testId: "kg-panel-run-link" } : words(title));
    if (p.run_at) line.push(words(` · started ${kgWhen(p.run_at)}`));
    if (step) line.push(words(` · ${step}`));
    lines.push(line);
  } else if (p.conversation_title || p.step === "chat") {
    lines.push([words(p.conversation_title ? `In the conversation “${p.conversation_title}”` : "In a conversation"),
                ...(p.conversation_at ? [words(` · ${kgWhen(p.conversation_at)}`)] : [])]);
  } else if (step) {
    lines.push([words(`At ${step}`)]);
  }
  if (p.model_id && !rawIdsIn(p.model_id).length) lines.push([words(`Model ${p.model_id}`)]);
  if (p.approved_by || p.approved_by_name) {
    lines.push([words("Approved by "), words(personName(p.approved_by, p.approved_by_name), { strong: true }),
                ...(p.approved_at ? [words(` · ${kgWhen(p.approved_at)}`)] : [])]);
  }
  if (p.version && p.version > 1) lines.push([words(`Version ${p.version}, edited from v${p.version - 1}`)]);
  return lines;
}

/** "Approved by Ada · Oct 24, 19:51 PDT" — the trust line under a rule's statement. */
export function approvalWords(p: KgProvenance): string | null {
  if (!p.approved_by && !p.approved_by_name) return null;
  return `Approved by ${personName(p.approved_by, p.approved_by_name)}${p.approved_at ? ` · ${kgWhen(p.approved_at)}` : ""}`;
}

/** A later run that used it: "Site Reliability Engineer in “B4 · Incident…” (cited, Oct 25, 07:20 PDT)". */
export function useWords(u: KgLineageUse): string {
  const who = agentName(u) ?? "an agent";
  const where = u.run_title && !rawIdsIn(u.run_title).length ? `“${u.run_title}”` : "a later run";
  return `${who} in ${where} (${u.how}${u.at ? `, ${kgWhen(u.at)}` : ""})`;
}

/** Plain text of lines, for a title attribute or a test. */
export function linesText(lines: ProvLine[]): string[] {
  return lines.map((l) => l.map((p) => (p.kind === "passport" ? "Passport" : p.text)).join(""));
}

/** An attribute a person reads (never a record key or an id-shaped value). */
const ATTR_RECORD_KEYS = /(^|_)(id|ids|key|keys|hash|ref|run|thread|sha)$/;
export function isWordsAttr(key: string, value: unknown): boolean {
  if (ATTR_RECORD_KEYS.test(key) || key === "injectable" || key === "seen_count" || key === "backfilled") return false;
  if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return false;
  return !rawIdsIn(String(value)).length;
}
