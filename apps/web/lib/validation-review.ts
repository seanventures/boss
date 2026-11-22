/**
 * VALIDATION AND REVIEW (docs/design/validation-review/SPEC.md, operator-approved 2026-11-21).
 *
 * The typed records the engine builds from step outputs, never from prose:
 *   boss.finding/v1        one issue from a validator, a reviewer seat, a test or a contract case
 *   boss.validation/v1     the Validate step's output: before → fixes → after (+ the findings)
 *   boss.review_packet/v2  what the merge card and the change page render (recommendation.packet)
 * and the §3 override calls. Every parser is total: a missing field becomes an empty value, so an
 * older engine draws a clean empty state, never a crash and never a raw id.
 */
import { IdentityError, idPost, refusalSentence } from "./identity/api";

export type Sev = "P0" | "P1" | "P2" | "P3";
export const SEVS: Sev[] = ["P0", "P1", "P2", "P3"];
export type FixKind = "auto" | "agent" | "human" | "none";
export type FindingStatus = "open" | "fixed" | "overridden" | "reverted" | "waived" | "suppressed";
export type OverrideAction = "accept" | "reject" | "edit" | "waive" | "reopen" | "revert_all" | "remove";

export type Evidence = { id: string; kind: string; cmd: string; exit: number | null; artifact: string; absent: boolean };
export type Override = {
  id: string; action: OverrideAction; finding_id: string;
  /** The actor's id, and the name the engine sent with it (empty: the directory names them). */
  actor: string; actor_name: string; reason: string; approver: string;
  approved_at: string; expires_at: string; at: string; commit_sha: string; rerun_id: string;
  before_text: string; after_text: string;
};
export type Finding = {
  id: string;
  source: { kind: string; name: string; rule_id: string; rule_doc: string };
  severity: Sev; basis: string; unmapped: boolean; native_severity: string;
  location: { file: string; line: number | null; end_line: number | null; partner_line: number | null };
  original: string | null; fixed: string | null; hunk_id: string; fix_kind: FixKind; status: FindingStatus;
  evidence: Evidence[]; override: Override | null;
  /** The rulebook's why and the ruleset's guide for the fixing agent, when the engine sends them. */
  why: string; guide: string;
  /** A reviewer finding BOSS moved to a follow-up (engine `deferred_to`: the follow-up build's
   *  id, or "queued"): shown open but not blocking. */
  followup: { change: number | null; href: string; build: string } | null;
  /** The validator's own message for it (masked). */
  title: string;
  /** Reported on the target only: the fix introduced it. */
  introduced: boolean;
  /** A reviewer finding that is NEVER a follow-up (a security HIGH on a P0 rule's lines): it
   *  blocks the merge like the P0 it sits on (engine `blocks`, verdict `security_review:R-n`). */
  blocks: boolean;
  /** "Needs you": the agent's change BOSS refused for a `human` / `never` rule (engine
   *  `proposed`). Accept commits it only while `applicable` (an engine `gone`/`valid: false`, or
   *  a 409 proposal_gone on Accept, makes it not): otherwise Edit or Remove. */
  proposed: { before: string | null; after: string | null; applicable: boolean } | null;
  /** The agent's lines are on the branch for a `human` / `never` rule: fixed only once a person accepts. */
  held: boolean;
};
/** One changed line of input → target (engine validators.record): `before` null = an added
 *  line, `after` null = a removed one; `finding` empty = a change no finding explains. */
export type Fix = { finding: string; rule: string; file: string; line: number | null; before: string | null; after: string | null; kind: FixKind; hunk_id: string };
export type RunResult = { command: string; exit: number | null; findings: string[]; head_sha: string; seconds: number | null; evidence: string };
export type ContractCase = { id: string; case: string; expect: number | string | null; got: number | string | null; ms: number | null; covers: string[] };
export type Validation = {
  validator: { id: string; version: string; adapter: string };
  ruleset: { id: string; version: number | null; sha: string };
  input: { file: string; sha256: string; from: string; text: string };
  target: string;
  before: RunResult; fixes: Fix[]; after: RunResult | null; findings: Finding[];
  overrides: Override[];
  /** The automatic re-run after an override (§3): Merge is disabled while it is pending. */
  rerun: { state: "idle" | "pending" | "failed"; started_at: string; eta_s: number | null; error: string };
  /** input_substituted and the like: the run validated the wrong bytes (SPEC §1.3). */
  guard: { code: string; source: string; why: string } | null;
  error: string;
  evidence: Evidence[];
  /** The newest packet, when GET …/validation sends it. */
  packet: ReviewPacket | null;
  head_sha: string;
  contract: ContractCase[];
  contract_target: string;
};
export type PacketCount = { found: number; open: number; waived: number };
/** Reviewer findings BOSS moved to a follow-up: their own "review" line, never in P0–P3 found. */
export type ReviewCount = { found: number; open: number; deferred: number; blocking: number };
export type ReviewPacket = {
  packet_version: number | null; head_sha: string; built_at: string;
  work: { title: string; workflow_run: string; run_no: number | null; build: string; later_builds: string[]; change: number | null };
  verdict: { state: "ready" | "blocked" | "invalid"; reasons: string[] };
  counts: Record<Sev, PacketCount> & { unmapped: number; review: ReviewCount | null };
  findings: Finding[];
  vs_input: { file: string; hunks: number; added: number; removed: number; hunks_by_finding: Record<string, string[]>; unexplained_hunks: string[] };
  validators: { id: string; ruleset: string; before: number; after_open: number; after_waived: number; exit: number | null; evidence: string }[];
  tests: { id: string; suite: string; passed: number; failed: number; evidence: string; ok: boolean | null }[];
  contract: ContractCase[];
  risks: { text: string; refs: string[] }[];
  claims: { text: string; refs: string[] }[];
  overrides: Override[];
  ruleset: { id: string; version: number | null; sha: string; stale: boolean };
  facts: number;
  /** The re-run after an override: pending/running holds Merge ("Re-checking…"). */
  rerun: { id: string; state: string } | null;
};

// ── total parsers ────────────────────────────────────────────────────────────────────────────

type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");
const nn = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const n = (v: unknown): number => nn(v) ?? 0;
const one = <T extends string>(v: unknown, ok: readonly T[], d: T): T => (ok.includes(v as T) ? (v as T) : d);
const strs = (v: unknown): string[] => a(v).map(s).filter(Boolean);

export const isSev = (v: unknown): v is Sev => SEVS.includes(v as Sev);

export function parseEvidence(v: unknown): Evidence {
  const x = o(v);
  return { id: s(x.id), kind: s(x.kind), cmd: s(x.cmd), exit: nn(x.exit), artifact: s(x.artifact), absent: x.absent === true };
}
export function parseOverride(v: unknown): Override {
  const x = o(v);
  return {
    // The label is the per-change O-n (`ref` on GET …/validation, `id` in the packet); the
    // global ovr_… id is the API's and is never shown.
    id: s(x.ref) || s(x.id), action: one(x.action, ["accept", "reject", "edit", "waive", "reopen", "revert_all", "remove"] as const, "accept"),
    finding_id: s(x.finding_id) || s(x.finding), actor: s(x.actor) || s(x.actor_name), actor_name: s(x.actor_name), reason: s(x.reason), approver: s(x.approver_name) || s(x.approver),
    approved_at: s(x.approved_at), expires_at: s(x.expires_at), at: s(x.created_at) || s(x.at), commit_sha: s(x.commit_sha),
    rerun_id: s(x.rerun_id), before_text: s(x.before_text), after_text: s(x.after_text),
  };
}
export function parseFinding(v: unknown): Finding {
  const x = o(v); const src = o(x.source); const loc = o(x.location); const fu = o(x.followup);
  const sev = x.severity;
  // §1.1: a finding with no basis is P2 + unmapped, so an unsupported P0 cannot block and an
  // unsupported P3 cannot hide.
  const unmapped = x.unmapped === true || !isSev(sev);
  return {
    id: s(x.id), source: { kind: s(src.kind), name: s(src.name), rule_id: s(src.rule_id), rule_doc: s(src.rule_doc) },
    severity: isSev(sev) ? sev : "P2", basis: s(x.basis), unmapped, native_severity: s(x.native_severity),
    location: { file: s(loc.file), line: nn(loc.line), end_line: nn(loc.end_line), partner_line: nn(loc.partner_line) },
    original: x.original == null ? null : s(x.original), fixed: x.fixed == null ? null : s(x.fixed),
    hunk_id: s(x.hunk_id), fix_kind: one(x.fix_kind, ["auto", "agent", "human", "none"] as const, "none"),
    status: one(x.status, ["open", "fixed", "overridden", "reverted", "waived", "suppressed"] as const, "open"),
    evidence: a(x.evidence).map(parseEvidence), override: x.override ? parseOverride(x.override) : null,
    why: s(x.why), guide: s(x.guide),
    followup: x.followup ? { change: nn(fu.change), href: s(fu.href), build: "" }
      // The engine's deferred_to: {by, number, href} (where it went), or older, the follow-up
      // build's id or a word ("queued", "later"): only an id is shown.
      : x.deferred_to && typeof x.deferred_to === "object" ? { change: nn(o(x.deferred_to).number), href: s(o(x.deferred_to).href), build: "" }
      : x.deferred_to ? { change: null, href: "", build: /^[a-z]+_[0-9a-z]+$/i.test(s(x.deferred_to)) ? s(x.deferred_to) : "" } : null,
    title: s(x.title), introduced: x.introduced === true, blocks: x.blocks === true,
    proposed: x.proposed && typeof x.proposed === "object"
      ? { before: o(x.proposed).before == null ? null : s(o(x.proposed).before),
          after: o(x.proposed).after == null ? null : s(o(x.proposed).after),
          applicable: o(x.proposed).applicable === true && o(x.proposed).gone !== true && o(x.proposed).valid !== false } : null,
    held: !!x.held,
  };
}
function parseRun(v: unknown): RunResult {
  const x = o(v);
  return { command: s(x.command), exit: nn(x.exit), findings: strs(x.findings), head_sha: s(x.head_sha), seconds: nn(x.seconds), evidence: s(x.evidence) };
}
function parseCase(v: unknown): ContractCase {
  const x = o(v);
  const val = (y: unknown) => (typeof y === "number" || typeof y === "string" ? y : null);
  return { id: s(x.id), case: s(x.case), expect: val(x.expect), got: val(x.got), ms: nn(x.ms), covers: strs(x.covers) };
}

/** boss.validation/v1 (+ overrides and re-run state from GET …/validation). Null when there is none. */
export function parseValidation(v: unknown): Validation | null {
  const root = o(v);
  const x = Object.keys(o(root.validation)).length ? { ...o(root.validation), ...pick(root, ["overrides", "rerun", "head_sha", "contract", "packet"]) } : root;
  const val = o(x.validator);
  if (!s(val.id) && !a(x.findings).length) return null;
  const rs = o(x.ruleset); const inp = o(x.input); const rr = o(x.rerun); const g = o(x.guard);
  const evidence = a(x.evidence).map(parseEvidence).filter((e) => e.id);
  // The engine names its evidence E-1 (before), E-2 (the fixer), E-3 (after).
  const evOf = (run: unknown, id: string) => (o(run).evidence ? o(run) : { ...o(run), evidence: evidence.some((e) => e.id === id) ? id : "" });
  return {
    validator: { id: s(val.id), version: s(val.version), adapter: s(val.adapter) },
    ruleset: { id: s(rs.id), version: nn(rs.version), sha: s(rs.sha) },
    input: { file: s(inp.file), sha256: s(inp.sha256), from: s(inp.from), text: s(inp.text) },
    target: s(x.target),
    before: parseRun(evOf(x.before, "E-1")),
    fixes: a(x.fixes).map((f) => {
      const y = o(f);
      return { finding: s(y.finding), rule: s(y.rule), file: s(y.file), line: nn(y.line),
               before: y.before == null ? null : s(y.before), after: y.after == null ? null : s(y.after),
               kind: one(y.kind, ["auto", "agent", "human", "none"] as const, "auto"), hunk_id: s(y.hunk_id) };
    }),
    after: x.after ? parseRun(evOf(x.after, "E-3")) : null,
    findings: a(x.findings).map(parseFinding).filter((f) => f.id),
    overrides: a(x.overrides).map(parseOverride).filter((r) => r.id),
    rerun: { state: rr.state === "pending" || rr.state === "running" ? "pending" : rr.state === "failed" ? "failed" : "idle",
             started_at: s(rr.started_at), eta_s: nn(rr.eta_s), error: s(rr.error) },
    guard: s(g.code) ? { code: s(g.code), source: s(g.source), why: s(g.why) } : null,
    error: s(x.error),
    evidence,
    packet: parsePacket(x.packet),
    head_sha: s(x.head_sha),
    contract: a(x.contract).map(parseCase).filter((c) => c.case),
    contract_target: s(x.contract_target),
  };
}
function pick(x: R, keys: string[]): R {
  return Object.fromEntries(keys.filter((k) => x[k] !== undefined).map((k) => [k, x[k]]));
}

/** boss.review_packet/v2, from a decision's recommendation (`packet`) or the packet itself. */
export function parsePacket(v: unknown): ReviewPacket | null {
  const x = o(v);
  if (!x.verdict && !x.findings && !x.counts) return null;
  const w = o(x.work); const vd = o(x.verdict); const c = o(x.counts); const vi = o(x.vs_input); const rs = o(x.ruleset);
  const findings = a(x.findings).map(parseFinding).filter((f) => f.id);
  const count = (k: Sev): PacketCount => { const y = o(c[k]); return { found: n(y.found), open: n(y.open), waived: n(y.waived) }; };
  const hbf: Record<string, string[]> = {};
  for (const [k, ids] of Object.entries(o(vi.hunks_by_finding))) hbf[k] = strs(ids);
  return {
    packet_version: nn(x.packet_version), head_sha: s(x.head_sha), built_at: s(x.built_at),
    work: { title: s(w.title), workflow_run: s(w.workflow_run), run_no: nn(w.run_no), build: s(w.build), later_builds: strs(w.later_builds), change: nn(w.change) },
    verdict: { state: one(vd.state, ["ready", "blocked", "invalid"] as const, "invalid"), reasons: strs(vd.reasons) },
    counts: { P0: count("P0"), P1: count("P1"), P2: count("P2"), P3: count("P3"), unmapped: n(c.unmapped),
              // The engine's own review line, when it sends one (counts.review).
              review: c.review && typeof c.review === "object"
                ? { found: n(o(c.review).found), open: n(o(c.review).open), deferred: n(o(c.review).deferred), blocking: n(o(c.review).blocking) } : null },
    findings,
    vs_input: { file: s(vi.file), hunks: n(vi.hunks), added: n(vi.added), removed: n(vi.removed), hunks_by_finding: hbf, unexplained_hunks: strs(vi.unexplained_hunks) },
    validators: a(x.validators).map((y) => { const z = o(y); return { id: s(z.id), ruleset: s(z.ruleset), before: n(z.before), after_open: n(z.after_open), after_waived: n(z.after_waived), exit: nn(z.exit), evidence: s(z.evidence) }; }),
    tests: a(x.tests).map((y) => { const z = o(y); return { id: s(z.id), suite: s(z.suite), passed: n(z.passed), failed: n(z.failed), evidence: s(z.evidence), ok: typeof z.ok === "boolean" ? z.ok : null }; }),
    contract: a(x.contract).map(parseCase).filter((k) => k.case),
    risks: a(x.risks).map((y) => ({ text: s(o(y).text), refs: strs(o(y).refs) })).filter((r) => r.text),
    claims: a(x.claims).map((y) => ({ text: s(o(y).text), refs: strs(o(y).refs) })).filter((r) => r.text),
    overrides: a(x.overrides).map(parseOverride).filter((r) => r.id),
    ruleset: { id: s(rs.id), version: nn(rs.version), sha: s(rs.sha), stale: rs.stale === true },
    facts: Array.isArray(x.cited_fact_ids) ? x.cited_fact_ids.length : n(x.facts),
    rerun: x.rerun ? { id: s(o(x.rerun).id), state: s(o(x.rerun).state) } : null,
  };
}

/** The packet on a merge decision (`recommendation.packet`, §1.4), or null. */
export function packetOf(rec: Record<string, unknown> | null | undefined): ReviewPacket | null {
  return rec ? parsePacket(rec.packet) : null;
}

// ── what the screens derive ─────────────────────────────────────────────────────────────────

const STATUS_RANK: Record<FindingStatus, number> = { open: 0, overridden: 2, reverted: 2, waived: 3, fixed: 4, suppressed: 5 };
const SEV_RANK: Record<Sev, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };
/** §1.4: open P0, open P1, then overridden, waived, fixed; severity, then id within each. */
export function sortFindings(list: Finding[]): Finding[] {
  // An open finding moved to a follow-up (not blocking) sits after the open ones that block.
  const rank = (f: Finding) => (f.status === "open" ? SEV_RANK[f.severity] + (f.followup && !f.blocks ? 5 : 0) : 10 + STATUS_RANK[f.status]);
  return [...list].sort((x, y) => {
    const ox = rank(x);
    const oy = rank(y);
    return ox - oy || SEV_RANK[x.severity] - SEV_RANK[y.severity] || x.id.localeCompare(y.id, undefined, { numeric: true });
  });
}
/** Severity, then rule, then line (§2 Findings column). */
export function groupBySeverity(list: Finding[]): { sev: Sev; items: Finding[] }[] {
  return SEVS.map((sev) => ({
    sev,
    items: list.filter((f) => f.severity === sev).sort((x, y) => (x.location.partner_line ?? x.location.line ?? 0) - (y.location.partner_line ?? y.location.line ?? 0) || x.source.rule_id.localeCompare(y.source.rule_id)),
  })).filter((g) => g.items.length);
}

export function countsOf(list: Finding[]): Record<Sev, PacketCount & { fixed: number; overridden: number }> {
  const out = {} as Record<Sev, PacketCount & { fixed: number; overridden: number }>;
  for (const sev of SEVS) {
    const of = list.filter((f) => f.severity === sev);
    out[sev] = { found: of.length, open: of.filter((f) => f.status === "open").length, waived: of.filter((f) => f.status === "waived").length,
                 fixed: of.filter((f) => f.status === "fixed").length, overridden: of.filter((f) => f.status === "overridden" || f.status === "reverted").length };
  }
  return out;
}

/** A review seat's finding: counted on its own "review" line, never in P0–P3 "found" (the engine's
 *  counts.review; P-levels count what validators, tests, contract cases and guards found). */
export function isReviewFinding(f: Finding): boolean {
  return f.source.kind === "reviewer" || f.source.kind === "review";
}

export type Tally = Record<Sev, PacketCount & { fixed: number; overridden: number }> & { review: ReviewCount; unmapped: number };

/**
 * THE ONE COUNT every validation surface shows (the Validation tab badge, the merge card's
 * severity cells and verdict, the change page, the Work panel). P0–P3 count the findings that
 * are not review follow-ups; review follow-ups are their own line. With findings, it is
 * recomputed from them (the engine's rule 3 says the two agree); a thin packet with counts only
 * uses the engine's counts.
 */
export function packetTally(p: ReviewPacket): Tally {
  if (!p.findings.length) {
    const out = {} as Tally;
    for (const sev of SEVS) out[sev] = { ...p.counts[sev], fixed: 0, overridden: 0 };
    return { ...out, review: p.counts.review ?? { found: 0, open: 0, deferred: 0, blocking: 0 }, unmapped: p.counts.unmapped };
  }
  const review = p.findings.filter(isReviewFinding);
  const open = review.filter((f) => f.status === "open" || f.status === "reverted");
  return {
    ...countsOf(p.findings.filter((f) => !isReviewFinding(f))),
    review: { found: review.length, open: open.length, deferred: open.filter((f) => f.followup && !f.blocks).length, blocking: open.filter((f) => f.blocks).length },
    unmapped: p.findings.filter((f) => f.unmapped).length || p.counts.unmapped,
  };
}

/** What blocks a merge: open P0 + P1, and a review finding that blocks. The Validation tab's badge. */
export const blockingOpen = (t: Tally): number => t.P0.open + t.P1.open + t.review.blocking;

/** The open rows a blocked packet's merge goes past (the merge card's and the change page's
 *  "Merge anyway" list): open P0/P1 that are not follow-ups, and blocking review findings. */
export function blockingRows(p: ReviewPacket | null): string[] {
  if (!p || p.verdict.state !== "blocked") return [];
  return p.findings.filter((f) => (f.status === "open" || f.status === "reverted")
      && (f.blocks || (!f.followup && !isReviewFinding(f) && (f.severity === "P0" || f.severity === "P1"))))
    .map((f) => `${f.severity} ${f.id} ${f.source.rule_id || f.source.name || ""}${lineWords(f) ? ` · ${lineWords(f)}` : ""}`.replace(/\s+/g, " ").trim());
}

/** The work's run and build, from the packet (what was validated) — one source for the merge
 *  card, the change page and the Work panel. Empty when there is no packet. */
export function workRefsOf(p: ReviewPacket | null): { run: string; run_no: number | null; build: string } {
  if (!p) return { run: "", run_no: null, build: "" };
  return { run: p.work.workflow_run.startsWith("wfr_") ? p.work.workflow_run : "", run_no: p.work.run_no,
           build: p.work.build.startsWith("crun_") ? p.work.build : "" };
}

/** A BOSS guard's finding (U-n: an unexplained security change): counted beside the validator's. */
export const isGuardFinding = (f: Finding): boolean => f.source.kind === "guard";

export type ValidationCounts = {
  /** What the validator found (V-n), and what BOSS's guards added (U-n). */
  found: number; guard: number;
  /** Fixed, by who: the fixer (--fix), the agent, a person's override. */
  auto: number; agent: number; human: number; waived: number;
  /** Still open (validator + guard), and of those, the ones only a person can close ("needs you"). */
  open: number; needYou: number;
};

/**
 * THE ONE VALIDATION COUNT the Work panel's funnel and the merge card's checks row both show
 * (dry run 2: the panel said "9 found → 6 open", the card "gwlint 9 → 5 open" — the guard's U-01
 * was in one and not the other). Guard findings count; review-seat findings have their own line
 * (packetTally). A record with no validator findings falls back to the step's `before` count.
 */
export function validationCounts(findings: Finding[], validatorFound = 0): ValidationCounts {
  const f = findings.filter((x) => !isReviewFinding(x));
  const fixedBy = (k: FixKind) => f.filter((x) => x.status === "fixed" || x.status === "overridden")
    .filter((x) => (x.override && (x.override.action === "edit" || x.override.action === "remove" || x.override.action === "reject") ? "human" : x.fix_kind) === k).length;
  const open = f.filter((x) => x.status === "open" || x.status === "reverted");
  const validator = f.filter((x) => !isGuardFinding(x)).length;
  return {
    found: validator || validatorFound, guard: f.filter(isGuardFinding).length,
    auto: fixedBy("auto"), agent: fixedBy("agent"), human: fixedBy("human"),
    waived: f.filter((x) => x.status === "waived").length,
    open: open.length, needYou: open.filter((x) => x.fix_kind === "human").length,
  };
}

/** The Built column's funnel (§5.3): the validation's counts, from the packet when it is there
 *  (the same findings the merge card reads), else from the step's own findings. */
export function funnelOf(v: Validation): ValidationCounts {
  return validationCounts(v.packet?.findings.length ? v.packet.findings : v.findings, v.before.findings.length);
}

/** "9 found + 1 guard → 6 open · 2 need you": the words both surfaces print. */
export function countsWords(c: ValidationCounts): string {
  return `${c.found} found${c.guard ? ` + ${c.guard} guard` : ""} → ${c.open} open${c.needYou ? ` · ${c.needYou} need${c.needYou === 1 ? "s" : ""} you` : ""}`;
}

export const SEV_WORDS: Record<Sev, string> = {
  P0: "exposes data or credentials · blocks merge",
  P1: "breaks availability or contract · blocks merge",
  P2: "hygiene · does not block",
  P3: "style · collapsed",
};
export const KIND_WORDS: Record<FixKind, string> = { auto: "auto", agent: "agent", human: "needs you", none: "—" };
export const STATUS_WORDS: Record<FindingStatus, string> = {
  open: "open", fixed: "fixed", overridden: "overridden", reverted: "reverted", waived: "waived", suppressed: "suppressed",
};

/** The line a person reads first: THE PARTNER'S (the file they sent, the gutter of the
 *  Validation tab), so the merge card, the Findings column and the fix cards all say "L17" for
 *  the same secret; the branch's line is the tooltip (lineTitle). A finding with no partner line
 *  (a reviewer's, on another file) shows the branch's. "L21–31", or "" — never "Lnull". */
export function lineWords(f: Pick<Finding, "location">): string {
  const { line, end_line, partner_line } = f.location;
  const span = line != null && end_line && end_line > line ? end_line - line : 0;
  const first = partner_line ?? line;
  if (first == null) return "";
  return span ? `L${first}–${first + span}` : `L${first}`;
}
/** The tooltip that says which file each number is in: the partner's line and the branch's. */
export function lineTitle(f: Pick<Finding, "location">): string {
  const { line, partner_line } = f.location;
  if (partner_line == null) return line != null ? `Line ${line} on the branch` : "";
  if (line == null) return `Line ${partner_line} of the partner's file (removed on the branch)`;
  return `Line ${partner_line} of the partner's file · line ${line} on the branch`;
}

/**
 * THE PARTNER → NOW CELL, said honestly (dry run 2: open V-05/V-07 read "→ (removed)", and V-06
 * read "allow_credentials: true → allow_credentials: true"). "(removed)" only for a finding that
 * is closed and whose line is gone; a line still on the branch is "unchanged"; a line the fix left
 * as it was says so and names the hunk that did fix it.
 */
export function partnerNow(f: Pick<Finding, "original" | "fixed" | "status" | "hunk_id">): { before: string; after: string; kind: "changed" | "removed" | "unchanged" | "added" } {
  const was = f.original && f.original !== "(none)" ? f.original : "";
  const closed = f.status === "fixed" || f.status === "overridden";
  if (f.fixed == null) {
    return closed ? { before: was || "(none)", after: "(removed)", kind: "removed" }
      : { before: was || "(none)", after: "unchanged", kind: "unchanged" };
  }
  if (!was) return { before: "(none)", after: f.fixed, kind: "added" };
  if (f.fixed.trim() === was.trim()) {
    return { before: was, after: closed && f.hunk_id ? `unchanged · fixed in ${f.hunk_id}` : "unchanged", kind: "unchanged" };
  }
  return { before: was, after: f.fixed, kind: "changed" };
}

/** What a person did, in the past tense: the words every override record reads. */
export const OVERRIDE_VERBS: Record<OverrideAction, string> = {
  accept: "accepted", reject: "rejected", edit: "edited", waive: "waived", reopen: "reopened",
  revert_all: "reverted the file", remove: "removed",
};

/**
 * THE LINES "Remove these lines" DELETES, from the partner's file: the finding's line, and when it
 * opens a block (a `- path:` list item, or a `key:` with children), every line nested under it.
 * Line numbers are the partner's (lineWords); `offset` maps them onto the branch.
 */
export function removalBlock(inputText: string, f: Pick<Finding, "location" | "original">): { from: number; to: number; lines: string[]; offset: number } | null {
  const { line, partner_line } = f.location;
  const offset = line != null && partner_line != null ? line - partner_line : 0;
  const all = inputText ? inputText.split("\n") : [];
  if (partner_line == null || partner_line < 1 || partner_line > all.length) {
    return f.original && f.original !== "(none)" && (partner_line ?? line) != null
      ? { from: (partner_line ?? line)!, to: (partner_line ?? line)!, lines: f.original.split("\n"), offset } : null;
  }
  const indent = (t: string) => t.length - t.trimStart().length;
  const head = all[partner_line - 1];
  const item = /^\s*-\s/.test(head);
  const opens = item || /:\s*$/.test(head);
  // Everything nested under it: a list item's keys and a key's children sit deeper than it.
  const base = indent(head);
  let to = partner_line;
  if (opens) {
    for (let i = partner_line; i < all.length; i += 1) {
      const t = all[i];
      if (!t.trim()) continue;
      if (indent(t) > base) { to = i + 1; continue; }
      break;
    }
  }
  return { from: partner_line, to, lines: all.slice(partner_line - 1, to), offset };
}

/** The title of the WORK, not of the newest follow-up: every "Follow-up: … — " prefix a follow-up
 *  build adds to its parent's title is dropped (SPEC §0 #2). */
export function rootTitle(title: string | null | undefined): string {
  let t = String(title ?? "").trim();
  for (let i = 0; i < 8; i += 1) {
    const m = /^(?:follow-?up|retry)(?:\s*\d+)?\s*:\s*[^—–]*?\s+[—–-]\s+/i.exec(t);
    if (!m) break;
    t = t.slice(m[0].length).trim();
  }
  return t.replace(/^(?:follow-?up|retry)(?:\s*\d+)?\s*:\s*/i, "").trim();
}

/** A short id for a long record id: crun_58aecc16…, never the whole thing. */
export const shortId = (id: string, keep = 8): string => {
  const m = /^([a-z]+_)([0-9a-z]+)$/i.exec(id);
  return m ? `${m[1]}${m[2].slice(0, keep)}` : id.slice(0, keep + 5);
};
export const shortSha = (sha: string) => sha.slice(0, 7);

/** Why a verdict says what it says: "open_p0:V-07" → { check: "open_p0", problem: "V-07" }. */
export function reasonRows(reasons: string[]): { check: string; problem: string }[] {
  return reasons.map((r) => {
    const rule = /^rule(\d+):([^:]*):(.*)$/.exec(r);
    if (rule) return { check: `rule ${rule[1]}${rule[2] ? ` · ${rule[2]}` : ""}`, problem: rule[3].trim() };
    const i = r.indexOf(":");
    return i > 0 ? { check: r.slice(0, i).trim(), problem: r.slice(i + 1).trim() } : { check: r, problem: "" };
  });
}

/** Two-line ids a ref chip can scroll to: every finding, evidence, override, test and case id. */
export function refTarget(id: string): string { return `vr-ref-${id.replace(/[^A-Za-z0-9_-]/g, "_")}`; }

// ── §3 override calls ────────────────────────────────────────────────────────────────────────

export type OverrideBody = { action: OverrideAction; text?: string; reason?: string; approver_id?: string; expires_at?: string; head_sha: string };
export type OverrideResult = { ok: boolean; status: number; missing: boolean; stale: boolean; error: string | null; body: R | null; code: string };

const enc = encodeURIComponent;
export const validationPath = (projectId: string, n: number | string) => `/v1/changes/${enc(projectId)}/${enc(String(n))}/validation`;
export const overridePath = (projectId: string, n: number | string, findingId: string) =>
  `/v1/changes/${enc(projectId)}/${enc(String(n))}/findings/${enc(findingId)}/override`;
export const revertAllPath = (projectId: string, n: number | string) => `${validationPath(projectId, n)}/revert-all`;
/** Assumed (not named in SPEC §3): re-run the validator and rebuild the packet at the head sha. */
export const rerunPath = (projectId: string, n: number | string) => `${validationPath(projectId, n)}/rerun`;

/** A route this engine does not serve yet: the control shows disabled with a tooltip. A 404
 *  the route itself wrote carries an `error` code (no_change, no_finding) and is a real answer. */
export const isMissingRoute = (status: number, body?: unknown) =>
  (status === 404 || status === 405 || status === 501) && !s(o(body).error);
/** Merge waits while the packet re-runs after an override. */
export const rerunPending = (p: ReviewPacket | null): boolean =>
  !!p && (p.rerun?.state === "pending" || p.rerun?.state === "running" || p.verdict.reasons.includes("rerun_pending"));
/** The checks ran on an older commit than the change's head (a push after them): the engine
 *  refuses the merge until a re-run at the head (verdict `stale_evidence`, or the heads differ). */
export const packetStale = (p: ReviewPacket | null, currentHead?: string | null): boolean =>
  !!p && (p.verdict.reasons.includes("stale_evidence")
    || (!!currentHead && !!p.head_sha && p.validators.length > 0 && !currentHead.startsWith(p.head_sha) && !p.head_sha.startsWith(currentHead)));

export async function sendOverride(path: string, body: R): Promise<OverrideResult> {
  try {
    const out = await idPost<R>(path, body);
    return { ok: true, status: 200, missing: false, stale: false, error: null, body: o(out), code: "" };
  } catch (e) {
    const status = e instanceof IdentityError ? e.status : 0;
    const b = e instanceof IdentityError ? o(e.body) : {};
    const code = s(b.error);
    const missing = isMissingRoute(status, b);
    const stale = status === 409 && code === "stale_head";
    return { ok: false, status, missing, stale, code, body: b,
             error: missing ? NOT_DEPLOYED : stale ? STALE : CODE_WORDS[code] ?? (s(b.message) || refusalSentence(e)) };
  }
}
export const STALE = "This change moved on; review the new version.";
/** Accept on a "Needs you" proposal the engine cannot apply any more (409 proposal_gone). */
export const PROPOSAL_GONE = "The agent's proposal is no longer valid; edit or remove instead.";
export const REMOVE_NOT_DEPLOYED = "Removing lines needs the latest BOSS: this engine cannot delete lines yet.";
/** The refusals that mean the proposal on screen cannot be applied. */
export const isProposalGone = (code: string) => code === "proposal_gone" || code === "edit_required";

/**
 * "REMOVE THESE LINES" (dry run 2, gap 2: nothing in the console could delete the admin route).
 * The engine takes it as `action: "remove"`, or — an engine that added the empty edit instead —
 * as `action: "edit"` with empty text; either form works. An engine with neither answers the
 * first with a 422 (an unknown action) and the second with `text_required`: then it says so.
 */
export async function sendRemove(path: string, body: { reason: string; head_sha: string }): Promise<OverrideResult> {
  const first = await sendOverride(path, { action: "remove", ...body });
  const unknownAction = !first.ok && first.status === 422 && (first.code === "bad_action" || !first.code);
  if (!unknownAction) return first;
  const second = await sendOverride(path, { action: "edit", text: "", ...body });
  if (!second.ok && second.code === "text_required") return { ...second, error: REMOVE_NOT_DEPLOYED };
  return second;
}
/** The engine's short refusals, said in full next to the row they belong to. */
const CODE_WORDS: Record<string, string> = {
  reason_required: "Write why first. The reason is recorded on the change.",
  text_required: "Write the replacement lines first.",
  rerun_pending: "BOSS is re-checking the last change. Try again when it finishes.",
  proposal_gone: PROPOSAL_GONE,
  edit_required: PROPOSAL_GONE,
  hunk_gone: "That change is no longer on the branch. Reload to see the newest version.",
};
export const NOT_DEPLOYED = "Overrides are not on this BOSS yet (the engine route is not deployed).";

// ── §4 rulesets ──────────────────────────────────────────────────────────────────────────────

export type Autofix = "mechanical" | "agent" | "human" | "never";
export const AUTOFIXES: Autofix[] = ["mechanical", "agent", "human", "never"];
export type RulesetRule = { id: string; enabled: boolean; severity: Sev; autofix: Autofix; guide: string; custom: boolean; check: string };
export type Ruleset = {
  id: string; version: number | null; sha: string; path: string; validator: string; merged_at: string; merged_by: string;
  rules: RulesetRule[]; rules_doc: string;
};
export function parseRuleset(v: unknown): Ruleset | null {
  const x = o(o(v).ruleset && typeof o(v).ruleset === "object" ? o(v).ruleset : v);
  if (!s(x.id) && !s(x.ruleset)) return null;
  const rules: RulesetRule[] = [];
  const asRule = (id: string, y: R, custom: boolean): RulesetRule => ({
    id, enabled: y.enabled !== false, severity: isSev(y.severity) ? y.severity : "P2",
    autofix: one(y.autofix, AUTOFIXES, "never"), guide: s(y.guide), custom, check: s(y.check),
  });
  if (Array.isArray(x.rules)) for (const r of x.rules) rules.push(asRule(s(o(r).id), o(r), o(r).custom === true));
  else for (const [id, r] of Object.entries(o(x.rules))) rules.push(asRule(id, o(r), false));
  for (const r of a(x.custom)) rules.push(asRule(s(o(r).id), o(r), true));
  return {
    id: s(x.id) || s(x.ruleset), version: nn(x.version), sha: s(x.sha), path: s(x.path) || `.boss/rulesets/${s(x.id) || s(x.ruleset)}.yaml`,
    validator: s(x.validator), merged_at: s(x.merged_at), merged_by: s(x.merged_by_name) || s(x.merged_by),
    rules: rules.filter((r) => r.id), rules_doc: s(x.rules_doc),
  };
}
export function parseRulesets(v: unknown): Ruleset[] {
  const x = o(v);
  const list = Array.isArray(v) ? v : a(x.rulesets);
  return list.map(parseRuleset).filter((r): r is Ruleset => r !== null);
}
export const rulesetsPath = (projectId: string) => `/v1/projects/${enc(projectId)}/rulesets`;
/** GET /v1/projects/{p}/validators: the base branch's `.boss/validators.yaml` (`exists: false`
 *  when there is none yet). Null from an engine without the route. */
export type ValidatorsFile = { exists: boolean; sha: string | null; path: string; ids: string[] };
export function parseValidatorsFile(v: unknown): ValidatorsFile | null {
  const x = o(v);
  if (typeof x.exists !== "boolean") return null;
  return { exists: x.exists, sha: s(x.sha) || null, path: s(x.path) || ".boss/validators.yaml",
           ids: a(x.validators).map((y) => s(o(y).id)).filter(Boolean) };
}
export const validatorsPath = (projectId: string) => `/v1/projects/${enc(projectId)}/validators`;
export const proposeValidatorsPath = (projectId: string) => `${validatorsPath(projectId)}/propose`;

// ── creating validation for a repo that has none (Settings › Validators) ────────────────────

export type ValidatorDecl = { id: string; applies_to: string[]; run: string; fix: string; adapter: string; rules_doc: string; ruleset: string };
export const ADAPTERS = ["gwlint-json", "sarif", "eslint-json", "ruff-json", "junit"];
/** `.boss/validators.yaml`'s one entry, pre-filled for a gateway config linter (SPEC §2). */
export function validatorTemplate(ruleset: string): ValidatorDecl {
  return { id: "gwlint", applies_to: ["config/tenants/*.yaml"], run: "go run ./cmd/gwlint validate {file} --json",
           fix: "go run ./cmd/gwlint --fix {file}", adapter: "gwlint-json", rules_doc: "rules/api-guidelines.yaml", ruleset };
}
/** A first ruleset (SPEC §4's P-levels): secrets, CORS with credentials, admin paths and plaintext
 *  upstreams are P0; missing limits P1; PII declaration P2. Edited later in the ruleset editor. */
export function rulesetTemplate(): RulesetRule[] {
  const r = (id: string, severity: Sev, autofix: Autofix, guide = ""): RulesetRule => ({ id, enabled: true, severity, autofix, guide, custom: false, check: "" });
  return [
    r("no-inlined-secrets", "P0", "never", "Delete the key if the route already authenticates another way; otherwise use a secret reference. Never write a value you computed."),
    r("cors-no-wildcard-with-credentials", "P0", "agent", "Default to allow_credentials: false. Keep credentials only if the brief names exact origins."),
    r("no-sensitive-paths-exposed", "P0", "human", "Removing a route is a product decision: propose it, a person accepts."),
    r("upstream-https-required", "P0", "mechanical"),
    r("tenant-rate-limit-required", "P1", "agent", "rps 50, burst 50 unless the brief names a quota."),
    r("rate-limit-required", "P1", "mechanical"),
    r("request-size-capped", "P1", "mechanical"),
    r("tenant-id-matches-filename", "P1", "never"),
    r("pii-fields-declared", "P2", "agent", "List the query params that carry personal data; ask the partner if unknown."),
  ];
}
/** A ruleset id from a project id: lower-case words and dashes. */
export const rulesetIdFor = (projectId: string) => (projectId.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "") || "default");
/** Create a ruleset: the propose route with no base (nothing to be stale against) and the
 *  validator it layers on; the engine opens a change ADDING `.boss/rulesets/<id>.yaml` (v1). */
export function rulesetCreateBody(rules: RulesetRule[], validator: string): Record<string, unknown> {
  return {
    rules: rules.map(({ id, enabled, severity, autofix, guide }) => ({ id, enabled, severity, autofix, guide })),
    base_version: null, base_sha: null, ...(validator ? { validator } : {}),
  };
}
/** Add a validator: POST …/validators/propose with the whole list (here, the one new entry) and
 *  the file's sha as the base (null: there is no file yet). */
export function validatorsBody(v: ValidatorDecl, baseSha: string | null): Record<string, unknown> {
  return {
    validators: [{ id: v.id, applies_to: v.applies_to, run: v.run, ...(v.fix ? { fix: v.fix } : {}), output: { adapter: v.adapter },
                   ...(v.rules_doc ? { rules_doc: v.rules_doc } : {}), ...(v.ruleset ? { ruleset: v.ruleset } : {}) }],
    base_sha: baseSha,
  };
}
/** An engine that cannot create the files yet: the ruleset route answers 404 (no ruleset) or
 *  refuses the `validator` field it does not know (422 extra field); the validators route is
 *  not there (404). */
export const needsNewerEngine = (status: number, error: string | null) =>
  status === 404 || status === 405 || status === 501 || (status === 422 && /extra|not permitted|validator/i.test(error ?? ""));
export const NEEDS_LATEST = "Needs the latest BOSS: this engine cannot create a ruleset or validators.yaml yet.";
export const proposeRulesetPath = (projectId: string, id: string) => `${rulesetsPath(projectId)}/${enc(id)}/propose`;

/** §4: lowering a P0, or turning on an auto-fix for one, needs a second approver. */
export function needsSecondApprover(was: RulesetRule, now: RulesetRule): boolean {
  if (was.severity !== "P0") return false;
  return now.severity !== "P0" || (was.autofix === "never" && now.autofix !== "never") || (was.enabled && !now.enabled);
}
/** The rules a proposal changes, with each field it changes. */
export function rulesetDiff(was: Ruleset, rules: RulesetRule[]): { id: string; fields: string[] }[] {
  const by = Object.fromEntries(was.rules.map((r) => [r.id, r]));
  return rules.map((r) => {
    const b = by[r.id];
    if (!b) return { id: r.id, fields: ["added"] };
    const fields = (["enabled", "severity", "autofix", "guide"] as const).filter((k) => b[k] !== r[k]);
    return { id: r.id, fields: [...fields] };
  }).filter((d) => d.fields.length);
}
