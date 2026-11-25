/**
 * A PROJECT THAT EXPLAINS ITSELF (engine services/project_experience/explained.py).
 *
 * `About`: the Overview's "About this app" card — the founding PRD's summary, its acceptance
 * criteria with the latest check's verdicts, who approved the spec and when, the spec's link; or
 * the project's brief when there is no PRD yet.
 * `Explained`: a work item's / build's "What was discussed / What was built" — the panel's
 * decisions and 3–6 bullets from the build's own record (files by purpose, tests, review,
 * follow-ups, the merge). One parser for the drawer and the build page.
 */

type Obj = Record<string, unknown>;
const o = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const strs = (v: unknown): string[] => a(v).map((x) => s(x)).filter(Boolean);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

export type CriterionStatus = "met" | "partial" | "not_met" | "unchecked";
export type AboutCriterion = { n: number; text: string; status: CriterionStatus; evidence: string | null };
export type About = {
  source: "prd" | "brief";
  title: string;
  summary: string | null;
  criteria: AboutCriterion[];
  counts: Record<CriterionStatus, number>;
  checkedAt: string | null;
  approved: { words: string; at: string | null } | null;
  specHref: string | null;
  note: string | null;
  later: { title: string; approvedAt: string | null; href: string }[];
  laterCount: number;
};

const STATUSES: CriterionStatus[] = ["met", "partial", "not_met", "unchecked"];

export function parseAbout(v: unknown): About | null {
  if (!v || typeof v !== "object") return null;
  const x = o(v);
  const source = x.source === "prd" ? "prd" : "brief";
  const criteria = a(x.criteria).map((c, i) => {
    const y = o(c);
    const st = STATUSES.includes(y.status as CriterionStatus) ? (y.status as CriterionStatus) : "unchecked";
    return { n: num(y.n) || i + 1, text: s(y.text), status: st, evidence: sn(y.evidence) };
  }).filter((c) => c.text);
  const counts = Object.fromEntries(STATUSES.map((k) => [k, criteria.filter((c) => c.status === k).length])) as Record<CriterionStatus, number>;
  const ap = o(x.approved);
  const later = a(x.later).map((l) => { const y = o(l); return { title: s(y.title), approvedAt: sn(y.approved_at), href: s(y.href) }; })
    .filter((l) => l.title);
  return {
    source, title: s(x.title), summary: sn(x.summary), criteria, counts, checkedAt: sn(x.checked_at),
    approved: s(ap.words) ? { words: s(ap.words), at: sn(ap.at) } : null,
    specHref: sn(x.spec_href), note: sn(x.note), later, laterCount: num(x.later_count) || later.length,
  };
}

/** "4 of 6 met · 2 partly" — the checklist's one line; null when nothing was judged yet. */
export function criteriaWords(about: Pick<About, "criteria" | "counts">): string | null {
  const total = about.criteria.length;
  if (!total) return null;
  if (about.counts.unchecked === total) return "Not checked yet";
  const bits = [`${about.counts.met} of ${total} met`];
  if (about.counts.partial) bits.push(`${about.counts.partial} partly`);
  if (about.counts.not_met) bits.push(`${about.counts.not_met} not met`);
  return bits.join(" · ");
}

export type FileGroup = { purpose: string; files: string[]; count: number };
export type MustHave = { n: number; text: string; verdict: CriterionStatus; evidence: string | null };
export type Built = {
  buildId: string; title: string; number: number | null; status: string;
  bullets: string[]; groups: FileGroup[]; filesChanged: number; followUps: string[];
  /** One plain sentence about the user-visible change (engine explained.summary_of), or null. */
  summary: string | null;
  /** The must-haves with their verdicts from the build's acceptance check (empty when none). */
  mustHaves: MustHave[];
  tests: { files: number; passed: boolean | null; count: number | null };
  review: { rounds: number; raised: number };
};
export type Discussed = { decisions: string[]; openQuestions: string[]; dissent: string | null; seats: number; href: string };
export type Explained = {
  discussed: Discussed | null;
  built: Built | null;
  later: { buildId: string; title: string; number: number | null; line: string }[];
};

export function parseExplained(v: unknown): Explained | null {
  if (!v || typeof v !== "object") return null;
  const x = o(v);
  const d = x.discussed && typeof x.discussed === "object" ? o(x.discussed) : null;
  const b = x.built && typeof x.built === "object" ? o(x.built) : null;
  const discussed: Discussed | null = d ? {
    decisions: strs(d.decisions), openQuestions: strs(d.open_questions), dissent: sn(d.dissent),
    seats: num(d.seats), href: s(d.href),
  } : null;
  const built: Built | null = b ? {
    buildId: s(b.build_id), title: s(b.title), number: typeof b.number === "number" ? b.number : null, status: s(b.status),
    bullets: strs(b.bullets),
    groups: a(b.groups).map((g) => { const y = o(g); return { purpose: s(y.purpose), files: strs(y.files), count: num(y.count) || strs(y.files).length }; })
      .filter((g) => g.purpose && g.count),
    filesChanged: num(b.files_changed), followUps: strs(b.follow_ups),
    summary: sn(b.summary),
    mustHaves: a(o(b.acceptance).items).map((m, i) => {
      const y = o(m);
      return { n: num(y.n) || i + 1, text: s(y.text), verdict: STATUSES.includes(y.verdict as CriterionStatus) ? (y.verdict as CriterionStatus) : "unchecked",
               evidence: sn(y.evidence) };
    }).filter((m) => m.text),
    tests: { files: a(o(b.tests).files).length, passed: typeof o(b.tests).passed === "boolean" ? (o(b.tests).passed as boolean) : null,
             count: typeof o(b.tests).count === "number" ? (o(b.tests).count as number) : null },
    review: { rounds: num(o(b.review).rounds), raised: num(o(b.review).raised) },
  } : null;
  const later = a(x.later).map((l) => { const y = o(l); return { buildId: s(y.build_id), title: s(y.title), number: typeof y.number === "number" ? y.number : null, line: s(y.line) }; })
    .filter((l) => l.buildId);
  const hasSaid = !!discussed && (discussed.decisions.length > 0 || discussed.openQuestions.length > 0);
  const hasBuilt = !!built && built.bullets.length > 0;
  if (!hasSaid && !hasBuilt) return null;
  return { discussed: hasSaid ? discussed : null, built: hasBuilt ? built : null, later };
}

/** Where the build page reads it (GET …/builds/{id}/explained). */
export const explainedPath = (projectId: string, runId: string) =>
  `/v1/projects/${encodeURIComponent(projectId)}/builds/${encodeURIComponent(runId)}/explained`;
