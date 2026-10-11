/**
 * A build's page below the strip (engine/boss_engine/services/build_detail.py): the Now summary,
 * the tool log, the log search and the Outputs' honest words. Pure functions, so the page's
 * sentences are tested without a browser.
 *
 *   summary   cockpit.summary: stage against its time limit, attempt, where the attempt was
 *             seeded from, the retry note, files so far, the last test result, the gates.
 *   tool log  GET /v1/code-tasks/{id}/tool-log: each call in one line (command or path, time,
 *             exit code, a failed call's error), clipped and redacted on the server.
 *   search    one index over the Story, the tool calls and the verify output; `errors` and
 *             `stack traces` are quick filters over the same lines.
 */
import { arr, num, obj, opt, str } from "./factory-floor";

// ── the summary ─────────────────────────────────────────────────────────────────────────────

export type VerifyResult = {
  runId: string; attempt: number; own: boolean; at?: string; ok: boolean | null;
  passed: number | null; failed: number | null; total: number | null; failing: string[];
};
export type GatesResult = { runId: string; attempt: number; own: boolean; at?: string; passed: string[]; failed: string[] };
export type Seeded = {
  fromRun: string; attempt: number | null; files: number | null; tests: number | null; at?: string;
  missing: boolean; why?: string; pending: boolean;
};
export type BuildSummary = {
  phase: string | null;
  stage: { step: string; title: string; startedAt?: string; roleKey: string | null } | null;
  timeLimitS: number | null;
  attempt: number;
  seeded: Seeded | null;
  note: string | null;
  files: string[]; filesCount: number; filesFinal: boolean;
  verify: VerifyResult | null;
  gates: GatesResult | null;
};

const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const strs = (v: unknown): string[] => arr(v).map((x) => String(x ?? "")).filter(Boolean);

/** `cockpit.summary`. An older API sends none: null, and the page shows no Now band. */
export function parseSummary(w: unknown): BuildSummary | null {
  if (!w || typeof w !== "object") return null;
  const o = obj(w);
  const st = o.stage ? obj(o.stage) : null;
  const sd = o.seeded ? obj(o.seeded) : null;
  const v = o.verify ? obj(o.verify) : null;
  const g = o.gates ? obj(o.gates) : null;
  return {
    phase: opt(o.phase) ?? null,
    stage: st && opt(st.title) ? { step: str(st.step), title: str(st.title), startedAt: opt(st.started_at), roleKey: opt(st.role_key) ?? null } : null,
    timeLimitS: optNum(o.time_limit_s),
    attempt: num(o.attempt, 1),
    seeded: sd && opt(sd.from_run) ? {
      fromRun: str(sd.from_run), attempt: optNum(sd.attempt), files: optNum(sd.files), tests: optNum(sd.tests),
      at: opt(sd.at), missing: sd.missing === true, why: opt(sd.why), pending: sd.pending === true,
    } : null,
    note: opt(o.note) ?? null,
    files: strs(o.files), filesCount: num(o.files_count), filesFinal: o.files_final === true,
    verify: v ? {
      runId: str(v.run_id), attempt: num(v.attempt, 1), own: v.own === true, at: opt(v.at),
      ok: typeof v.ok === "boolean" ? v.ok : null,
      passed: optNum(v.passed), failed: optNum(v.failed), total: optNum(v.total), failing: strs(v.failing),
    } : null,
    gates: g ? { runId: str(g.run_id), attempt: num(g.attempt, 1), own: g.own === true, at: opt(g.at),
                 passed: strs(g.passed), failed: strs(g.failed) } : null,
  };
}

export function clock(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function mins(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** `7/11 failed` · `11/11 passed` · `Failed` (a runner that printed no counts). */
export function testsWord(v: Pick<VerifyResult, "passed" | "failed" | "total" | "ok">): string {
  const total = v.total ?? ((v.passed ?? 0) + (v.failed ?? 0) || null);
  if (v.failed && total) return `${v.failed}/${total} failed`;
  if (v.failed === 0 && total) return `${total}/${total} passed`;
  return v.ok === true ? "Passed" : v.ok === false ? "Failed" : "Ran";
}

/** Whose result a line is: nothing for this attempt's own, `#4` for an earlier one. */
const whose = (r: { own: boolean; attempt: number }) => (r.own ? "" : `#${r.attempt}`);

export type NowFact = {
  key: "stage" | "attempt" | "seeded" | "note" | "files" | "tests" | "gates" | "cost";
  label: string; value: string; tip?: string;
  tone?: "bad" | "good" | "warn";
  /** Shown under the value: the failing tests' names, the failed gates. */
  detail?: string[];
};

type NowInput = {
  summary: BuildSummary | null;
  state: { word: string; state: string; station: string | null };
  stations: { key: string; label: string }[];
  live: boolean;
  startedAt?: string; updatedAt?: string;
  lineage: { attempt: number; sameLineage: boolean }[];
  costUsd: number; metered: boolean; costWord: string;
};

/**
 * The Now band's facts, in reading order. Every fact is said only when BOSS recorded it; an
 * earlier attempt's result says whose it is (`#4`). `now` is the page clock (ms).
 */
export function nowFacts(c: NowInput, now: number | null): NowFact[] {
  const s = c.summary;
  const out: NowFact[] = [];
  const station = c.stations.find((x) => x.key === c.state.station)?.label;
  // Stage, against its time limit while an agent runs it; the whole build's time otherwise.
  const stage = s?.stage && c.live ? s.stage : null;
  if (stage) {
    const spent = stage.startedAt && now ? Math.max(0, (now - Date.parse(stage.startedAt)) / 1000) : null;
    const limit = s?.timeLimitS ?? null;
    const value = [station, stage.title, spent !== null ? (limit ? `${mins(spent)} of ${mins(limit)}` : mins(spent)) : ""]
      .filter(Boolean).join(" · ");
    out.push({ key: "stage", label: "Now", value,
      tip: limit ? `This stage stops at ${mins(limit)} (the agent time limit)` : "The stage running now",
      tone: spent !== null && limit && spent > limit * 0.8 ? "warn" : undefined });
  } else {
    const took = c.startedAt ? (Date.parse(c.live ? new Date(now ?? Date.now()).toISOString() : c.updatedAt ?? c.startedAt) - Date.parse(c.startedAt)) / 1000 : null;
    out.push({ key: "stage", label: "Now", value: [c.state.word, station && !c.state.word.includes(station) ? station : "", took !== null && took > 0 ? mins(took) : ""]
      .filter(Boolean).join(" · "), tip: s?.phase ? `Phase: ${s.phase}` : undefined,
      tone: c.state.state === "failed" ? "bad" : undefined });
  }
  const attempts = Math.max(1, ...c.lineage.filter((l) => l.sameLineage).map((l) => l.attempt));
  const attempt = s?.attempt ?? 1;
  out.push({ key: "attempt", label: "Attempt", value: `${attempt} of ${Math.max(attempt, attempts)}`,
             tip: "This build's place among its retries" });
  if (s?.seeded) {
    const sd = s.seeded;
    const from = sd.attempt ? `#${sd.attempt}` : "an earlier attempt";
    const value = sd.missing ? `Nothing saved in ${from}`
      : sd.pending ? `From ${from} · seeding`
      : [`From ${from}`, sd.files !== null ? `${sd.files} files` : "", sd.tests !== null ? `${sd.tests} tests` : ""].filter(Boolean).join(" · ");
    out.push({ key: "seeded", label: "Seeded", value,
      tip: sd.missing ? sd.why ?? "No saved workspace: this attempt runs the full pipeline"
        : `Continues from ${from}'s saved workspace: its plan and tests are kept`,
      tone: sd.missing ? "warn" : undefined });
  }
  if (s?.note) out.push({ key: "note", label: "Note", value: `“${s.note}”`, tip: "Your note on the retry" });
  if (s && (s.filesCount || c.live)) {
    out.push({ key: "files", label: "Files", value: s.filesCount ? `${s.filesCount} ${s.filesFinal ? "changed" : "so far"}` : "None yet",
               tip: s.files.length ? s.files.slice(0, 12).join("\n") + (s.filesCount > 12 ? `\n+${s.filesCount - 12} more` : "") : undefined });
  }
  if (s?.verify) {
    const v = s.verify;
    const value = [testsWord(v), whose(v), v.at ? `at ${clock(v.at)}` : ""].filter(Boolean).join(" · ");
    out.push({ key: "tests", label: "Tests", value, tone: v.ok === false || (v.failed ?? 0) > 0 ? "bad" : v.ok ? "good" : undefined,
               tip: v.own ? "This attempt's last test run" : `Attempt ${v.attempt}'s last test run`,
               detail: v.failing.length ? v.failing : undefined });
  } else if (c.live) {
    out.push({ key: "tests", label: "Tests", value: "Not run yet", tip: "BOSS runs the tests after the build" });
  }
  if (s?.gates) {
    const g = s.gates;
    const value = [g.failed.length ? `${g.failed.length} failed` : "", `${g.passed.length} passed`, whose(g)].filter(Boolean).join(" · ");
    out.push({ key: "gates", label: "Gates", value, tone: g.failed.length ? "bad" : "good",
               tip: [g.own ? "This attempt's gates" : `Attempt ${g.attempt}'s gates`, ...g.passed.map((x) => `✓ ${x}`), ...g.failed.map((x) => `✗ ${x}`)].join("\n"),
               detail: g.failed.length ? g.failed : undefined });
  }
  const cost = c.metered && c.costUsd > 0 ? `$${c.costUsd.toFixed(2)}` : c.costWord && c.costWord !== "Not yet" ? c.costWord : c.live ? "None yet" : "None";
  out.push({ key: "cost", label: "Cost", value: cost, tip: c.metered ? "Metered model spend so far" : "Model spend so far" });
  return out;
}

// ── the Outputs, honest about what is missing ────────────────────────────────────────────────

type OutputsInput = NowInput & {
  outputs: {
    pr: { word: string } | null;
    tests: { word: string; commands: unknown[] } | null;
    changes: { word: string } | null;
    preview: { word: string } | null;
    cost: { word: string };
  };
};

/** The word an Outputs row shows when the server has none, or says only `Not yet`: why it is
 *  missing, from what the build is doing. `null` keeps the server's word. */
export function missingWord(key: "pr" | "tests" | "changes" | "preview" | "cost", c: OutputsInput): { word: string; tip?: string } | null {
  const s = c.summary;
  const st = c.state.state;
  const running = c.live;
  if (key === "pr") {
    if (c.outputs.pr?.word) return null;
    if (running) return { word: "Held until the gates pass", tip: "BOSS opens the PR after the tests, checks and review pass" };
    if (st === "cancelled") return { word: "Not opened · stopped" };
    if (st === "failed") return { word: "Not opened · gates failed", tip: s?.gates?.failed.length ? s.gates.failed.join(", ") : undefined };
    return null;
  }
  if (key === "tests") {
    const own = s?.verify?.own ? s.verify : null;
    if (own && (own.total || own.failed !== null)) {
      return { word: `${testsWord(own)}${own.at ? ` at ${clock(own.at)}` : ""}`, tip: c.outputs.tests?.word };
    }
    if (c.outputs.tests?.commands.length) return null;
    const earlier = s?.verify && !s.verify.own ? s.verify : null;
    if (earlier) {
      return { word: `${testsWord(earlier)} at ${clock(earlier.at)} · #${earlier.attempt}`,
               tip: running ? `Attempt ${earlier.attempt}'s run. This attempt's tests run after the build.` : `Attempt ${earlier.attempt}'s run` };
    }
    if (running) return { word: "After the build", tip: "BOSS runs the tests when the coding stage ends" };
    return null;
  }
  if (key === "changes") {
    if (c.outputs.changes?.word) return null;
    if (s?.filesCount) return { word: `${s.filesCount} ${s.filesFinal ? "files" : "files so far"}`, tip: s.files.slice(0, 12).join("\n") };
    if (running) return { word: "None yet" };
    return null;
  }
  if (key === "preview") {
    if (c.outputs.preview?.word) return null;
    if (running && !c.outputs.pr?.word) return { word: "After the PR", tip: "BOSS starts a preview once the PR is open" };
    return null;
  }
  if (key === "cost") {
    if (c.outputs.cost.word && c.outputs.cost.word !== "Not yet") return null;
    return { word: running ? "None metered yet" : "None" };
  }
  return null;
}

// ── the tool log ────────────────────────────────────────────────────────────────────────────

export type ToolCall = {
  i: number; actId: string | null; tool: string; arg: string | null; ok: boolean | null; ms: number | null;
  exitCode: number | null; error: string | null; output: string | null; at?: string; clipped: boolean;
};
export type ToolLog = {
  items: ToolCall[]; total: number; more: boolean;
  verify: { runId: string; attempt: number; own: boolean; at?: string; output: string; clipped: boolean } | null;
};

export function parseToolLog(w: unknown): ToolLog {
  const o = obj(w);
  const v = o.verify ? obj(o.verify) : null;
  return {
    items: arr(o.items).map((x, i) => {
      const t = obj(x);
      return { i, actId: opt(t.act_id) ?? null, tool: str(t.tool, "tool"), arg: opt(t.arg) ?? null,
               ok: typeof t.ok === "boolean" ? t.ok : null, ms: optNum(t.ms), exitCode: optNum(t.exit_code),
               error: opt(t.error) ?? null, output: opt(t.output) ?? null, at: opt(t.at), clipped: t.clipped === true };
    }),
    total: num(o.total), more: o.more === true,
    verify: v && opt(v.output) ? { runId: str(v.run_id), attempt: num(v.attempt, 1), own: v.own !== false, at: opt(v.at),
                                   output: str(v.output), clipped: v.clipped === true } : null,
  };
}

/** `1.2s` · `340ms` · `2m 4s`. */
export function msWord(ms: number | null): string {
  if (ms === null) return "";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

/** The one line of a failed call's output that says why: the runner's own summary
 *  (`7 failed, 4 passed`), else the first line that names an error, else the first line. */
export function errorLine(output: string): string {
  const lines = output.split("\n").map((l) => l.replace(/<\/?tool_use_error>/g, "").replace(/^[=\-_ ]{4,}|[=\-_ ]{4,}$/g, "").trim())
    .filter((l) => l && !/^exit code\s*-?\d+$/i.test(l));
  const summary = [...lines].reverse().find((l) => /\b\d+\s+(?:failed|failing)\b/i.test(l));
  return summary ?? lines.find((l) => ERROR_RE.test(l) || STACK_RE.test(l)) ?? lines[0] ?? "";
}

/** The end of a call's line: `exit 1 · 7 failed, 4 passed in 3.21s`, or nothing for a clean call. */
export function outcomeWord(t: ToolCall): string {
  if (t.ok === null) return "running";
  if (t.ok) return t.exitCode ? `exit ${t.exitCode}` : "";
  return [t.exitCode !== null ? `exit ${t.exitCode}` : "failed", errorLine(t.error ?? "").slice(0, 120)].filter(Boolean).join(" · ");
}

const READ_LIKE = new Set(["read", "glob", "grep", "ls", "view"]);

export type ToolGroup = { kind: "one"; call: ToolCall } | { kind: "reads"; tool: string; calls: ToolCall[] };

/** Consecutive successful reads of the same kind fold into one line (`Read ×4`); every other
 *  call, and any failed read, stands alone. */
export function groupTools(calls: ToolCall[]): ToolGroup[] {
  const out: ToolGroup[] = [];
  for (const call of calls) {
    const readish = READ_LIKE.has(call.tool.toLowerCase()) && call.ok !== false;
    const last = out[out.length - 1];
    if (readish && last) {
      if (last.kind === "reads" && last.tool === call.tool) { last.calls.push(call); continue; }
      if (last.kind === "one" && last.call.tool === call.tool && last.call.ok !== false && READ_LIKE.has(call.tool.toLowerCase())) {
        out[out.length - 1] = { kind: "reads", tool: call.tool, calls: [last.call, call] };
        continue;
      }
    }
    out.push({ kind: "one", call });
  }
  return out;
}

// ── search ──────────────────────────────────────────────────────────────────────────────────

export type LogSource = "story" | "tool" | "verify";
export type LogLine = {
  id: string; source: LogSource;
  /** Where it is: `Repair · Bash pytest -q`, `Verify output #4`, `Build · Coding`. */
  where: string;
  text: string;
  /** For a tool line: the call it belongs to (a jump opens it). */
  call?: number;
  /** The line came from a failed tool call. */
  failed?: boolean;
};

/** A stack frame or the line that starts or ends one, in the languages BOSS builds. */
export const STACK_RE = /^\s*Traceback \(most recent call last\)|^\s+at\s+\S|^\s*File ".+", line \d+|\b[A-Z][A-Za-z]*(?:Error|Exception):|^\s*Error:|\bFAILED\b|✘|✗|^\s*(?:×|✕)\s|^\s*---\s+FAIL:|^panic:/;
/** A line that says something went wrong. */
export const ERROR_RE = /\b(?:error|errors|failed|failure|fatal|exception|panic|refused|denied)\b|✘|✗|\bexit (?:code )?[1-9]\d*\b|\bFAIL\b|\bnot ok\b/i;

export type QuickFilter = "errors" | "stack" | null;

export function isStackLine(line: string): boolean { return STACK_RE.test(line); }
export function isErrorLine(line: string): boolean { return ERROR_RE.test(line) || STACK_RE.test(line); }

type StoryLike = { id: string; type: string; verb?: string; title?: string; text?: string; said?: string | null;
  files?: string[] | null; failure?: { line: string } | null; what?: string; station?: string | null; detail?: Record<string, unknown> };

/** Every searchable line of the build, in page order: the Story, each tool call (its command
 *  or path, then its output or error, line by line), then the verify output. */
export function buildIndex(story: StoryLike[], log: ToolLog | null, stationLabel: (k: string | null | undefined) => string,
                           actTitle: (actId: string | null) => string): LogLine[] {
  const out: LogLine[] = [];
  for (const r of story) {
    const where = [stationLabel(r.station), r.type === "act" ? r.verb : ""].filter(Boolean).join(" · ") || "Story";
    const texts: string[] = [];
    if (r.type === "act") {
      texts.push([r.verb, r.title].filter(Boolean).join(" · "));
      if (r.said) texts.push(r.said);
      for (const f of r.files ?? []) texts.push(f);
      if (r.failure?.line) texts.push(r.failure.line);
    } else {
      if (r.text) texts.push(r.text);
      const cmds = Array.isArray(r.detail?.commands) ? (r.detail?.commands as Record<string, unknown>[]) : [];
      for (const c of cmds) texts.push(`${String(c.command ?? "")} · exit ${String(c.exit_code ?? "?")}`);
    }
    texts.forEach((t, k) => { if (t.trim()) out.push({ id: `s:${r.id}:${k}`, source: "story", where, text: t }); });
  }
  for (const t of log?.items ?? []) {
    const where = [actTitle(t.actId), t.tool].filter(Boolean).join(" · ");
    out.push({ id: `t:${t.i}:h`, source: "tool", where, text: [t.tool, t.arg, outcomeWord(t)].filter(Boolean).join(" · "),
               call: t.i, failed: t.ok === false });
    const body = t.ok === false ? t.error : t.output;
    (body ?? "").split("\n").forEach((line, k) => {
      if (line.trim()) out.push({ id: `t:${t.i}:${k}`, source: "tool", where, text: line, call: t.i, failed: t.ok === false });
    });
  }
  if (log?.verify) {
    const where = log.verify.own ? "Verify output" : `Verify output · #${log.verify.attempt}`;
    log.verify.output.split("\n").forEach((line, k) => {
      if (line.trim()) out.push({ id: `v:${k}`, source: "verify", where, text: line });
    });
  }
  return out;
}

/** The lines a query and a quick filter select, in page order. Case-insensitive; a filter alone
 *  selects every line it matches. Nothing is selected by an empty query and no filter. */
export function searchLines(index: LogLine[], query: string, filter: QuickFilter, cap = 500): LogLine[] {
  const q = query.trim().toLowerCase();
  if (!q && !filter) return [];
  const out: LogLine[] = [];
  for (const line of index) {
    if (filter === "stack" && !isStackLine(line.text)) continue;
    if (filter === "errors" && !(isErrorLine(line.text) || (line.failed && line.id.endsWith(":h")))) continue;
    if (q && !line.text.toLowerCase().includes(q) && !line.where.toLowerCase().includes(q)) continue;
    out.push(line);
    if (out.length >= cap) break;
  }
  return out;
}

/** A line cut where the query matches, for <mark>: [text, isMatch][]. */
export function highlight(text: string, query: string): [string, boolean][] {
  const q = query.trim().toLowerCase();
  if (!q) return [[text, false]];
  const out: [string, boolean][] = [];
  const low = text.toLowerCase();
  let from = 0;
  for (let at = low.indexOf(q); at !== -1; at = low.indexOf(q, at + q.length)) {
    if (at > from) out.push([text.slice(from, at), false]);
    out.push([text.slice(at, at + q.length), true]);
    from = at + q.length;
  }
  if (from < text.length) out.push([text.slice(from), false]);
  return out.length ? out : [[text, false]];
}

/** The next match to jump to, wrapping: `dir` 1 forward, -1 back. */
export function step(current: number, count: number, dir: 1 | -1): number {
  if (count <= 0) return -1;
  if (current < 0) return dir === 1 ? 0 : count - 1;
  return (current + dir + count) % count;
}
