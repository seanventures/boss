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
export type OverrideAction = "accept" | "reject" | "edit" | "waive" | "reopen" | "revert_all";

export type Evidence = { id: string; kind: string; cmd: string; exit: number | null; artifact: string; absent: boolean };
export type Override = {
  id: string; action: OverrideAction; finding_id: string; actor: string; reason: string; approver: string;
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
export type ReviewPacket = {
  packet_version: number | null; head_sha: string; built_at: string;
  work: { title: string; workflow_run: string; run_no: number | null; build: string; later_builds: string[]; change: number | null };
  verdict: { state: "ready" | "blocked" | "invalid"; reasons: string[] };
  counts: Record<Sev, PacketCount> & { unmapped: number };
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
    id: s(x.ref) || s(x.id), action: one(x.action, ["accept", "reject", "edit", "waive", "reopen", "revert_all"] as const, "accept"),
    finding_id: s(x.finding_id) || s(x.finding), actor: s(x.actor_name) || s(x.actor), reason: s(x.reason), approver: s(x.approver_name) || s(x.approver),
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
      // The engine's deferred_to is the follow-up build's id, or a word ("queued", "later"): only an id is shown.
      : x.deferred_to ? { change: null, href: "", build: /^[a-z]+_[0-9a-z]+$/i.test(s(x.deferred_to)) ? s(x.deferred_to) : "" } : null,
    title: s(x.title), introduced: x.introduced === true,
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
    counts: { P0: count("P0"), P1: count("P1"), P2: count("P2"), P3: count("P3"), unmapped: n(c.unmapped) },
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
  return [...list].sort((x, y) => {
    const ox = x.status === "open" ? SEV_RANK[x.severity] : 10 + STATUS_RANK[x.status];
    const oy = y.status === "open" ? SEV_RANK[y.severity] : 10 + STATUS_RANK[y.status];
    return ox - oy || SEV_RANK[x.severity] - SEV_RANK[y.severity] || x.id.localeCompare(y.id, undefined, { numeric: true });
  });
}
/** Severity, then rule, then line (§2 Findings column). */
export function groupBySeverity(list: Finding[]): { sev: Sev; items: Finding[] }[] {
  return SEVS.map((sev) => ({
    sev,
    items: list.filter((f) => f.severity === sev).sort((x, y) => (x.location.line ?? 0) - (y.location.line ?? 0) || x.source.rule_id.localeCompare(y.source.rule_id)),
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

/** The Built column's funnel (§5.3): found → auto · agent · you · waived → open. */
export function funnelOf(v: Validation): { found: number; auto: number; agent: number; human: number; waived: number; open: number } {
  const f = v.findings;
  const fixedBy = (k: FixKind) => f.filter((x) => x.fix_kind === k && (x.status === "fixed" || x.status === "overridden")).length;
  return {
    found: v.before.findings.length || f.length,
    auto: fixedBy("auto"), agent: fixedBy("agent"), human: fixedBy("human"),
    waived: f.filter((x) => x.status === "waived").length,
    open: f.filter((x) => x.status === "open").length,
  };
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

/** "L17", "L19–20", or "" — never "Lnull". */
export function lineWords(f: Pick<Finding, "location">): string {
  const { line, end_line } = f.location;
  if (line == null) return "";
  return end_line && end_line !== line ? `L${line}–${end_line}` : `L${line}`;
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
             error: missing ? NOT_DEPLOYED : stale ? STALE : s(b.message) || refusalSentence(e) };
  }
}
export const STALE = "This change moved on; review the new version.";
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
