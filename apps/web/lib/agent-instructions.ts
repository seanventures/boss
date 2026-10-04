/**
 * An agent's instructions and skills-from-a-file (agent-loadout spec §4.7), the console's half.
 *
 * Instructions are operator-written text — a review checklist, house rules — that goes into
 * every prompt the agent gets, in the modes it is for. They shape how it works, never what it
 * may do; the API lints them (refusing override, reach and gate-skipping text, and secrets) and
 * every change is a new agent version. A SKILL.md becomes a `guide` skill the agent reads only
 * when it needs it.
 *
 * Reads go through the same-origin `/v1` proxy; writes through apiWrite (one press, one
 * effect). A refusal resolves to `{ok: false, error, tip, line}` — the short words for the red
 * line, the full reason for its tooltip — never a throw.
 */
import { apiWrite } from "./api-write";

export type InstructionMode = "work" | "chat" | "panel";
export const MODES: InstructionMode[] = ["work", "chat", "panel"];
export const MODE_LABEL: Record<InstructionMode, string> = { work: "Work", chat: "Chat", panel: "Panels" };

/** The limits the API enforces (agent_instructions.py); the API's own answer wins when given. */
export const LIMITS = { docMaxChars: 16_000, budgetChars: 16_000, maxDocs: 8, fileMaxBytes: 65_536 };
const TEXT_FILE = /\.(md|markdown|txt)$/i;

export type InstructionItem = {
  id: string; name: string; source: "file" | "pasted" | "skill_md";
  sizeChars: number; tokensEst: number; sha256: string; modes: InstructionMode[];
  position: number; preview: string; updatedAt: string | null; updatedBy: string;
  sinceVersion: number | null; usable: boolean; unusableReason: string | null;
};
export type Budget = Record<InstructionMode, { usedChars: number; limitChars: number }>;
export type InstructionList = {
  items: InstructionItem[]; budget: Budget; version: number | null; canEdit: boolean;
};
export type DryRun = {
  tokensEst: number; sizeChars: number; budgetAfter: Budget; warnings: string[]; looksLikeSkill: boolean;
};
export type Saved = { item: InstructionItem; version: number; warnings: string[] };
export type PromptBlock = { layer: string; label: string; text: string };
export type VersionDoc = { name: string; sha256: string; modes: string[]; body: string };

export type Refused = { ok: false; error: string; tip?: string; line?: number; field?: string; status: number };
export type Answer<T> = { ok: true; data: T } | Refused;

type Wire = Record<string, unknown>;
const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? (v as Wire) : {});
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const num = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const enc = encodeURIComponent;

function toItem(w: unknown): InstructionItem {
  const o = obj(w);
  return {
    id: str(o.id), name: str(o.name), source: (str(o.source, "pasted") as InstructionItem["source"]),
    sizeChars: num(o.size_chars), tokensEst: num(o.tokens_est), sha256: str(o.sha256),
    modes: (Array.isArray(o.modes) ? o.modes : []).filter((m): m is InstructionMode => MODES.includes(m as InstructionMode)),
    position: num(o.position), preview: str(o.preview), updatedAt: typeof o.updated_at === "string" ? o.updated_at : null,
    updatedBy: str(o.updated_by), sinceVersion: typeof o.since_version === "number" ? o.since_version : null,
    usable: o.usable !== false, unusableReason: typeof o.unusable_reason === "string" ? o.unusable_reason : null,
  };
}

function toBudget(w: unknown): Budget {
  const o = obj(w);
  const one = (m: InstructionMode) => ({ usedChars: num(obj(o[m]).used_chars), limitChars: num(obj(o[m]).limit_chars, LIMITS.budgetChars) });
  return { work: one("work"), chat: one("chat"), panel: one("panel") };
}

/** The API's refusal, as the sheet shows it: ≤ 6 words, the reason in the tip. */
export function refusal(body: unknown, status: number): Refused {
  const b = obj(body);
  const d = b.detail ?? b.error;
  const dd = obj(d);
  const message = typeof d === "string" ? d : str(dd.message);
  const error = message || (status === 403 ? "Needs agent-editor rights" : status === 0 ? "Can't reach BOSS API · Retry" : `HTTP ${status}`);
  return {
    ok: false, error, status,
    ...(str(dd.tip) ? { tip: str(dd.tip) } : {}),
    ...(typeof dd.line === "number" ? { line: dd.line } : {}),
    ...(str(dd.field) ? { field: str(dd.field) } : {}),
  };
}

async function get<T>(path: string, map: (w: unknown) => T): Promise<Answer<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return refusal(body, r.status);
    return { ok: true, data: map(body) };
  } catch {
    return { ok: false, error: "Can't reach BOSS API · Retry", status: 0 };
  }
}

/** `label` names the operator's press; "" for an automatic call (a dry run), which takes no
 *  press scope and so can never block one. */
async function send<T>(path: string, method: "POST" | "PATCH" | "DELETE", body: Wire | null,
                       map: (w: unknown) => T, label: string): Promise<Answer<T>> {
  try {
    const r = await apiWrite(path, {
      method, headers: { "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}),
    }, label ? { press: { scope: `instructions-${label}`, label } } : {});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return refusal(data, r.status);
    return { ok: true, data: map(data) };
  } catch {
    return { ok: false, error: "Can't reach BOSS API · Retry", status: 0 };
  }
}

/** A dry run's refusal arrives as 200 {ok: false, field, message, line, tip}: an answer. */
async function dry(p: Promise<Answer<DryRun & { refused?: Wire }>>): Promise<Answer<DryRun>> {
  const r = await p;
  if (r.ok && r.data.refused) return refusal({ detail: r.data.refused }, 422);
  return r;
}

const toDryRun = (w: unknown): DryRun & { refused?: Wire } => {
  if (obj(w).ok === false) return { refused: obj(w) } as DryRun & { refused?: Wire };
  const o = obj(w);
  return { tokensEst: num(o.tokens_est), sizeChars: num(o.size_chars), budgetAfter: toBudget(o.budget_after),
           warnings: (Array.isArray(o.warnings) ? o.warnings : []).map(String), looksLikeSkill: o.looks_like_skill === true };
};

const saved = (w: unknown): Saved => {
  const o = obj(w);
  return { item: toItem(o.item), version: num(o.version, 1),
           warnings: (Array.isArray(o.warnings) ? o.warnings : []).map((x) => String(x)) };
};

export const instructionsApi = {
  list: (projectId: string, roleKey: string, actor: string) =>
    get(`/v1/agents/${enc(roleKey)}/instructions?project_id=${enc(projectId)}&actor=${enc(actor)}`, (w) => {
      const o = obj(w);
      return { items: (Array.isArray(o.items) ? o.items : []).map(toItem), budget: toBudget(o.budget),
               version: typeof o.version === "number" ? o.version : null, canEdit: o.can_edit === true } as InstructionList;
    }),
  body: (projectId: string, roleKey: string, id: string) =>
    get(`/v1/agents/${enc(roleKey)}/instructions/${enc(id)}?project_id=${enc(projectId)}`, (w) => str(obj(w).body)),
  dryRun: (projectId: string, actor: string, roleKey: string, input: { name: string; body: string; modes: InstructionMode[]; source: string }) =>
    dry(send(`/v1/agents/${enc(roleKey)}/instructions`, "POST",
      { project_id: projectId, actor, ...input, dry_run: true }, toDryRun, "")),  // automatic, not a press: it never blocks Add
  add: (projectId: string, actor: string, roleKey: string, input: { name: string; body: string; modes: InstructionMode[]; source: string }) =>
    send(`/v1/agents/${enc(roleKey)}/instructions`, "POST", { project_id: projectId, actor, ...input }, saved, "Add"),
  update: (projectId: string, actor: string, roleKey: string, id: string,
           changes: { body?: string; name?: string; modes?: InstructionMode[]; position?: number }) =>
    send(`/v1/agents/${enc(roleKey)}/instructions/${enc(id)}`, "PATCH", { project_id: projectId, actor, ...changes }, saved, "Save"),
  /** A replace, checked and not stored (the sheet's estimate for `Replace file`). */
  dryRunUpdate: (projectId: string, actor: string, roleKey: string, id: string, changes: { body?: string; modes?: InstructionMode[] }) =>
    dry(send(`/v1/agents/${enc(roleKey)}/instructions/${enc(id)}`, "PATCH", { project_id: projectId, actor, ...changes, dry_run: true },
      toDryRun, "")),
  remove: (projectId: string, actor: string, roleKey: string, id: string) =>
    send(`/v1/agents/${enc(roleKey)}/instructions/${enc(id)}?project_id=${enc(projectId)}&actor=${enc(actor)}`,
      "DELETE", null, (w) => ({ version: num(obj(w).version, 1) }), "Remove"),
  prompt: (projectId: string, actor: string, roleKey: string, mode: InstructionMode) =>
    get(`/v1/agents/${enc(roleKey)}/prompt?project_id=${enc(projectId)}&mode=${mode}&actor=${enc(actor)}`,
      (w) => (Array.isArray(obj(w).blocks) ? (obj(w).blocks as unknown[]) : []).map((b) => ({
        layer: str(obj(b).layer), label: str(obj(b).label), text: str(obj(b).text) })) as PromptBlock[]),
  atVersion: (projectId: string, roleKey: string, version: number) =>
    get(`/v1/agents/${enc(roleKey)}/versions/${version}/instructions?project_id=${enc(projectId)}`,
      (w) => (Array.isArray(obj(w).items) ? (obj(w).items as unknown[]) : []).map((i) => ({
        name: str(obj(i).name), sha256: str(obj(i).sha256), body: str(obj(i).body),
        modes: (Array.isArray(obj(i).modes) ? obj(i).modes as unknown[] : []).map(String) })) as VersionDoc[]),
  skillFromFile: (projectId: string, actor: string, input: { filename: string; text: string; label?: string; summary?: string; grantTo?: string }) =>
    send("/v1/skills/from-file", "POST", {
      project_id: projectId, actor, filename: input.filename, text: input.text,
      ...(input.label ? { label: input.label } : {}), ...(input.summary !== undefined ? { summary: input.summary } : {}),
      ...(input.grantTo ? { grant_to: input.grantTo } : {}),
    }, (w) => {
      const o = obj(w);
      const skill = obj(o.skill);
      return { skillKey: str(skill.skill_key), label: str(skill.label), version: typeof o.version === "number" ? o.version : null };
    }, "Create"),
};

// ── Pure helpers (tested) ───────────────────────────────────────────────────────────────────

/** ~4 characters a token, as the API counts. */
export const tokensOf = (chars: number): number => Math.ceil(Math.max(0, chars) / 4);

/** `1.2k` / `780` / `4k`: the meter's numbers. */
export function kTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  const k = tokens / 1000;
  return `${Number.isInteger(Math.round(k * 10) / 10) ? Math.round(k) : (Math.round(k * 10) / 10).toFixed(1)}k`;
}

/** `1.2k / 4k tokens` for the fullest mode. */
export function meterLine(budget: Budget | null): string {
  if (!budget) return `— / ${kTokens(tokensOf(LIMITS.budgetChars))} tokens`;
  const used = Math.max(...MODES.map((m) => budget[m].usedChars));
  const limit = Math.max(...MODES.map((m) => budget[m].limitChars || LIMITS.budgetChars));
  return `${kTokens(tokensOf(used))} / ${kTokens(tokensOf(limit))} tokens`;
}

/** Full when any mode is at its budget, or at the most files. */
export function isFull(list: InstructionList | null): boolean {
  if (!list) return false;
  if (list.items.length >= LIMITS.maxDocs) return true;
  return MODES.some((m) => list.budget[m].usedChars >= (list.budget[m].limitChars || LIMITS.budgetChars));
}

/** `3.1 KB` — as the row shows the size. */
export function sizeLabel(chars: number): string {
  if (chars < 1024) return `${chars} B`;
  return `${(chars / 1024).toFixed(1)} KB`;
}

/** The first line of a body, as the row's tip (≤ 80). */
export function firstLine(body: string): string {
  const line = (body.split("\n").find((l) => l.trim()) ?? "").replace(/^#+\s*/, "").trim();
  return line.length > 80 ? `${line.slice(0, 79)}…` : line;
}

/** SKILL.md frontmatter: `---`, `name:`, `description:`, `---`. */
export function frontmatter(body: string): { name?: string; description?: string } | null {
  const m = /^﻿?---[ \t]*\n([\s\S]*?)\n---[ \t]*(?:\n|$)/.exec(body);
  if (!m) return null;
  const out: Record<string, string> = {};
  for (const line of m[1].split("\n")) {
    const at = line.indexOf(":");
    if (at > 0 && !/^\s/.test(line)) out[line.slice(0, at).trim().toLowerCase()] = line.slice(at + 1).trim().replace(/^["']|["']$/g, "");
  }
  return { name: out.name, description: out.description };
}
export const looksLikeSkill = (body: string): boolean => {
  const f = frontmatter(body);
  return !!(f?.name && f?.description);
};

/**
 * A picked file, checked before anything is sent: a .md/.markdown/.txt, under the byte ceiling,
 * no NUL. The server re-checks everything; this only saves a pointless request.
 */
export async function readTextFile(file: File): Promise<{ ok: true; text: string; name: string } | { ok: false; error: string }> {
  if (/\.(zip|tar|gz|tgz)$/i.test(file.name)) return { ok: false, error: "One .md file for now" };
  if (!TEXT_FILE.test(file.name)) return { ok: false, error: "Not a text file" };
  if (file.size > LIMITS.fileMaxBytes) return { ok: false, error: "Too long · 16k max" };
  const text = await file.text();
  if (text.includes("\u0000")) return { ok: false, error: "Not a text file" };
  if (text.length > LIMITS.docMaxChars * 1.25) return { ok: false, error: "Too long · 16k max" };
  return { ok: true, text, name: file.name };
}

/** `2h ago`, `just now` — the row's age. `now` is passed so tests are stable. */
export function ago(iso: string | null, now: number): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
