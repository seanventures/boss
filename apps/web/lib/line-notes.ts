/**
 * VALIDATION AS A CHECKER (docs/design/workflow-library/DESIGN.md decision 4). The typed
 * boss.validation/v1 record, read as a checker like tests or the reviewer:
 *   Checks         one row per checker: "9 found → 0 open (4 tool · 3 agent · 2 you)"
 *   Files changed  each finding is a note pinned on its line of the diff
 * Pure helpers only; the components are app/validation/checks-rows.tsx and line-notes.tsx.
 */
import type { DiffFile } from "./diff-view";
import { isGuardFinding, isReviewFinding, type Finding, type Sev } from "./validation-review";

/** The engine's first commit on a "fix a given file" change (input_fidelity.commit_original). */
export const ORIGINAL_COMMIT_PREFIX = "Original file as given";
export const isOriginalCommit = (message: string | null | undefined): boolean =>
  String(message ?? "").trimStart().startsWith(ORIGINAL_COMMIT_PREFIX);

/** The commit that put the original file on the change, when the engine made one. */
export function originalCommit<T extends { sha: string; message: string }>(commits: T[]): T | null {
  return commits.find((c) => isOriginalCommit(c.message)) ?? null;
}

/** Who closed it, or who must: the person's word on the note and in the Checks row. */
export type By = "tool" | "agent" | "you";
export function ownerOf(f: Finding): By {
  const person = f.override && (f.override.action === "edit" || f.override.action === "remove" || f.override.action === "reject");
  if (person || f.status === "waived" || f.status === "overridden") return "you";
  if (f.fix_kind === "auto") return "tool";
  if (f.fix_kind === "agent") return "agent";
  return "you";
}

export const isOpen = (f: Finding): boolean => f.status === "open" || f.status === "reverted";

/** "fixed by tool" · "fixed by agent" · "fixed by you" · "Needs you" · "Open" · "Waived". */
export function noteWords(f: Finding, record: Finding["override"] = f.override): { text: string; tone: "you" | "done" | "open" | "dim" } {
  if (isOpen(f)) {
    if (record?.action === "waive" && !record.approved_at) return { text: "Waive waits for approval", tone: "you" };
    return f.fix_kind === "human" || isGuardFinding(f) || f.status === "reverted" || !!f.proposed
      ? { text: "Needs you", tone: "you" } : { text: "Open", tone: "open" };
  }
  if (f.status === "waived") return { text: "Waived", tone: "dim" };
  if (f.status === "suppressed") return { text: "Suppressed", tone: "dim" };
  return { text: `fixed by ${ownerOf(f)}`, tone: "done" };
}

export type CheckerCount = { found: number; open: number; tool: number; agent: number; you: number; waived: number; p0p1Open: number };

export function checkerCount(findings: Finding[]): CheckerCount {
  const c: CheckerCount = { found: findings.length, open: 0, tool: 0, agent: 0, you: 0, waived: 0, p0p1Open: 0 };
  for (const f of findings) {
    c[ownerOf(f)] += 1;
    if (f.status === "waived") c.waived += 1;
    if (isOpen(f)) {
      c.open += 1;
      if (f.severity === "P0" || f.severity === "P1") c.p0p1Open += 1;
    }
  }
  return c;
}

/** "9 found → 0 open (4 tool · 3 agent · 2 you)"; "0 found" when the checker found nothing. */
export function checkerWords(c: CheckerCount): string {
  if (!c.found) return "0 found";
  const by = (["tool", "agent", "you"] as const).filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(" · ");
  return `${c.found} found → ${c.open} open${by ? ` (${by})` : ""}`;
}

const SEV_RANK: Record<Sev, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };
/** P0 first, then P1…; open before closed inside a severity; then by line. */
export function bySeverity(list: Finding[]): Finding[] {
  return [...list].sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity]
    || Number(isOpen(b)) - Number(isOpen(a))
    || (a.location.line ?? a.location.partner_line ?? 0) - (b.location.line ?? b.location.partner_line ?? 0)
    || a.id.localeCompare(b.id, undefined, { numeric: true }));
}

/** A checker's findings, grouped: each validator by name, BOSS's guards with the first validator,
 *  the review seats as "Reviewer", anything else by its source kind (a security scan, a test). */
export type Checker = { key: string; name: string; kind: "validator" | "reviewer" | "other"; findings: Finding[] };
export function checkersOf(findings: Finding[], validatorIds: string[]): Checker[] {
  const ids = [...new Set(validatorIds.filter(Boolean))];
  const out = new Map<string, Checker>();
  const put = (key: string, name: string, kind: Checker["kind"], f?: Finding) => {
    const c = out.get(key) ?? { key, name, kind, findings: [] };
    if (f) c.findings.push(f);
    out.set(key, c);
  };
  for (const id of ids) put(`v:${id}`, id, "validator");
  for (const f of findings) {
    if (isReviewFinding(f)) put("reviewer", "Reviewer", "reviewer", f);
    else if (isGuardFinding(f)) put(`v:${ids[0] ?? "guard"}`, ids[0] ?? "BOSS guard", "validator", f);
    else if (f.source.kind === "validator" || !f.source.kind) {
      const id = f.source.name || ids[0] || "validator";
      put(`v:${id}`, id, "validator", f);
    } else {
      const k = f.source.kind;
      put(`k:${k}`, /secur|vuln|scan/i.test(k) ? "Security scan" : f.source.name || k, "other", f);
    }
  }
  return [...out.values()];
}

/**
 * WHERE EACH NOTE SITS in one file's diff: a finding's branch line on the new side; a line the
 * fix removed on the old side (the original file, when the diff starts from it); else loose,
 * shown at the top of the file so nothing is lost. Keys are "new:<n>" and "old:<n>".
 */
export function placeNotes(findings: Finding[], file: DiffFile, target = ""): { at: Map<string, Finding[]>; loose: Finding[] } {
  const mine = findings.filter((f) => (f.location.file || target) === file.path);
  const news = new Set<number>(); const olds = new Set<number>();
  for (const h of file.hunks) for (const l of h.lines) {
    if (l.newNo != null && l.kind !== "note") news.add(l.newNo);
    if (l.oldNo != null && l.kind === "del") olds.add(l.oldNo);
  }
  const at = new Map<string, Finding[]>();
  const loose: Finding[] = [];
  const add = (k: string, f: Finding) => at.set(k, [...(at.get(k) ?? []), f]);
  for (const f of bySeverity(mine)) {
    const { line, partner_line } = f.location;
    if (line != null && news.has(line)) add(`new:${line}`, f);
    else if (partner_line != null && olds.has(partner_line) && file.status !== "added") add(`old:${partner_line}`, f);
    else loose.push(f);
  }
  return { at, loose };
}
