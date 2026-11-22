"use client";

/**
 * CHANGE › CHECKS: ONE ROW PER CHECKER (docs/design/workflow-library/DESIGN.md decision 4,
 * wireframe screen 2; the content of validation-review wireframe B and C). The build, the tests,
 * each CI run, each validator (gwlint), a security scan, the reviewer and the contract tests read
 * the same way: icon · name · "9 found → 0 open (4 tool · 3 agent · 2 you)". A validator's row
 * opens to its summary: the funnel and exit code, the severity bar, the findings (P0/P1 table:
 * ID · rule · line · original → now · fix · status, each line a link into Files changed), the
 * overrides, the open risks and a link to its Rules. Validation has no page of its own.
 */
import React from "react";

import type { ChangeSet } from "../../lib/repo-links";
import { checkerCount, checkersOf, checkerWords, isOpen, noteWords, type Checker } from "../../lib/line-notes";
import {
  isReviewFinding, OVERRIDE_VERBS, parsePacket, refTarget, shortSha, type ContractCase, type Finding, type Override, type ReviewPacket, type Validation,
} from "../../lib/validation-review";
import { ActorName, RefChip } from "./kit";
import { ChangedVsPartner, FindingsTable, SevBar } from "./review-packet";

type Tone = "ok" | "you" | "bad" | "wait";
const ICON: Record<Tone, string> = { ok: "✓", you: "!", bad: "✗", wait: "…" };

function Icon({ tone }: { tone: Tone }) {
  return <span className="ck-ic" data-tone={tone} aria-label={tone === "ok" ? "passed" : tone === "wait" ? "running" : tone === "you" ? "needs you" : "failed"}>{ICON[tone]}</span>;
}

function Plain({ tone, name, words, testid }: { tone: Tone; name: string; words: string; testid?: string }) {
  return (
    <div className="ck" data-testid={testid ?? "ck-row"} data-tone={tone}>
      <div className="ck-sum"><Icon tone={tone} /><b>{name}</b><span className="ck-words">{words}</span></div>
    </div>
  );
}

export type JumpTo = (path: string, line?: number | null, side?: "new" | "old") => void;

/** "O-1 · Priya Raman · accepted V-07 · “why” · 14:19 · re-run E-3": one row per override. */
function OverrideRows({ rows }: { rows: Override[] }) {
  if (!rows.length) return null;
  const time = (iso: string) => { const d = iso ? new Date(iso) : null; return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""; };
  return (
    <div data-testid="ck-overrides">
      <div className="vr-k">Overrides · {rows.length}</div>
      <ul className="ck-ovr">{rows.map((r) => (
        <li key={r.id} id={refTarget(r.id)} data-testid="vr-override-row" data-action={r.action}>
          <span className="vr-mono vr-id">{r.id}</span>
          <ActorName id={r.actor} name={r.actor_name} />
          <span>{OVERRIDE_VERBS[r.action]}{r.finding_id ? ` ${r.finding_id}` : ""}</span>
          {r.reason ? <span className="vr-muted ck-why" title={r.reason}>“{r.reason}”</span> : null}
          {r.at ? <span className="vr-dim vr-mono">{time(r.at)}</span> : null}
          {r.rerun_id ? (/^[A-Z]-\d+$/.test(r.rerun_id) ? <RefChip id={r.rerun_id} label={`re-run ${r.rerun_id}`} /> : <span className="vr-dim">re-run</span>) : null}
        </li>
      ))}</ul>
    </div>
  );
}

/** The runs behind the count (wireframe B): "$ gwlint validate … · exit 1 · E-1", the re-run at head. */
function RunLines({ v }: { v: Validation }) {
  const line = (r: Validation["before"], label: string, testid: string) => (r.command ? (
    <div className="vr-mono vr-dim vr-tiny" data-testid={testid}>{label} $ {r.command}
      {[r.exit != null ? `exit ${r.exit}` : "", r.head_sha ? `at head ${shortSha(r.head_sha)}` : "",
        label === "Re-run" && v.ruleset.version != null ? `ruleset v${v.ruleset.version}` : "", r.seconds != null ? `${r.seconds} s` : ""]
        .filter(Boolean).map((x) => ` · ${x}`).join("")} {r.evidence ? <RefChip id={r.evidence} /> : null}</div>) : null);
  return <div className="ck-runs">{line(v.before, "Run", "ck-run-before")}{v.after ? line(v.after, "Re-run", "vr-rerun-result") : null}</div>;
}

function ContractRow({ cases, target }: { cases: ContractCase[]; target: string }) {
  const pass = cases.filter((x) => x.got != null && String(x.got) === String(x.expect)).length;
  const tone: Tone = pass === cases.length ? "ok" : "bad";
  return (
    <details className="ck" data-tone={tone} data-testid="ck-contract">
      <summary className="ck-sum"><Icon tone={tone} /><b>Contract tests</b>
        <span className="ck-words">{pass} / {cases.length} pass{target ? ` · ${target}` : ""}</span></summary>
      <div className="ck-body"><table>
        <thead><tr><th>Case</th><th>Expect</th><th>Got</th><th>Covers</th></tr></thead>
        <tbody>{cases.map((c) => (
          <tr key={c.id} id={refTarget(c.id)} data-testid="ck-case">
            <td className="vr-mono">{c.case}</td><td className="vr-mono">{c.expect ?? "—"}</td>
            <td className="vr-mono" style={{ color: String(c.got) === String(c.expect) ? "var(--v2-green)" : "var(--v2-red)" }}>{c.got ?? "—"}</td>
            <td>{c.covers.length ? c.covers.map((id) => <span key={id} className="vr-ref">{id}</span>) : <span className="vr-dim">—</span>}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </details>
  );
}

function CheckerRow({ c, v, packet, target, onJump, rechecking, rulesHref }: {
  c: Checker; v: Validation | null; packet: ReviewPacket | null; target: string; onJump: JumpTo; rechecking: boolean; rulesHref: string;
}) {
  const n = checkerCount(c.findings);
  const tone: Tone = rechecking ? "wait" : !n.open ? "ok" : c.findings.some((f) => isOpen(f) && noteWords(f).tone === "you") ? "you" : "bad";
  const words = c.kind === "reviewer" ? `${n.found} found → ${n.open} open` : checkerWords(n);
  const validator = c.kind === "validator";
  const mine = validator && v && (!v.validator.id || v.validator.id === c.name) ? v : null;
  if (!c.findings.length) {
    return (
      <div className="ck" data-tone="ok" data-testid="ck-checker" data-checker={c.name}>
        <div className="ck-sum"><Icon tone="ok" /><b>{c.name}</b><span className="ck-words" data-testid="ck-words">0 found</span>
          <span className="cs-spacer" />{validator && rulesHref ? <a href={rulesHref} className="ck-rules" data-testid="ck-rules">Rules</a> : null}</div>
      </div>
    );
  }
  // This checker's findings as a packet: the severity bar counts them the one way (packetTally).
  const sub = { ...(parsePacket({ verdict: { state: "ready" } }) as ReviewPacket), findings: c.findings };
  const jump = (f: Finding) => {
    const path = f.location.file || target;
    const line = f.location.line ?? f.location.partner_line;
    if (path && line != null) onJump(path, line, f.location.line != null ? "new" : "old");
  };
  const ids = new Set(c.findings.map((f) => f.id));
  const overrides = (mine?.overrides.length ? mine.overrides : packet?.overrides ?? []).filter((r) => !r.finding_id || ids.has(r.finding_id));
  const exit = mine ? (mine.after?.exit ?? mine.before.exit) : null;
  const you = n.you - n.waived;
  return (
    <details className="ck" data-tone={tone} data-testid="ck-checker" data-checker={c.name} open={n.open > 0 || undefined}>
      <summary className="ck-sum"><Icon tone={tone} /><b>{c.name}</b>
        <span className="ck-words" data-testid="ck-words">{words}</span>
        {rechecking ? <span className="vr-st" data-st="pending">re-checking…</span> : null}</summary>
      <div className="ck-body">
        <div className="ck-funnel vr-mono" data-testid="ck-funnel">
          <b>{n.found}</b> found <span className="vr-dim">→</span> {n.tool} tool · {n.agent} agent · {you} you{n.waived ? ` · ${n.waived} waived` : ""}
          <span className="vr-dim"> →</span> <b data-tone={n.open ? "red" : "green"}>{n.open}</b> open
          {exit != null ? <span className="vr-dim"> · exit {exit}</span> : null}
          <span className="cs-spacer" />
          {validator && rulesHref ? <a href={rulesHref} className="ck-rules" data-testid="ck-rules">Rules</a> : null}
        </div>
        {mine ? <RunLines v={mine} /> : null}
        <SevBar p={sub} />
        <FindingsTable findings={c.findings} onJump={jump} testid={`ck-table-${c.key}`} />
        {validator && packet ? <ChangedVsPartner p={packet} /> : null}
        <OverrideRows rows={overrides} />
        {validator && packet?.risks.length ? (
          <div data-testid="vr-risks">
            <div className="vr-k">Open risks</div>
            {packet.risks.map((r) => <div key={r.text} className="vr-risk">{r.text} {r.refs.map((id) => <RefChip key={id} id={id} />)}</div>)}
          </div>
        ) : null}
      </div>
    </details>
  );
}

/** Every checker of the change, in one list: build, tests, CI runs, validators, scans, reviewer. */
export function CheckRows({ cs, v, packet, onJump, projectId = "" }: {
  cs: ChangeSet; v: Validation | null; packet: ReviewPacket | null; onJump: JumpTo; projectId?: string;
}) {
  const c = cs.checks;
  const tests = c?.tests_passed;
  const findings = packet?.findings.length ? packet.findings : v?.findings ?? [];
  const ids = [v?.validator.id ?? "", ...(packet?.validators.map((x) => x.id) ?? [])];
  const checkers = checkersOf(findings, ids);
  const rechecking = v?.rerun.state === "pending";
  const cases = v?.contract.length ? v.contract : packet?.contract ?? [];
  const suites = packet?.tests ?? [];
  const reviewOnly = !findings.some(isReviewFinding) && packet?.counts.review?.found;
  // Settings › Validators: the ruleset editor.
  const rulesHref = projectId ? `/?${new URLSearchParams({ project_id: projectId, area: "project", tab: "settings", section: "validators" })}` : "";
  return (
    <div className="vr ck-rows" data-testid="ck-rows">
      {c?.run_status != null ? (
        <Plain tone={/fail|error|cancel/i.test(c.run_status ?? "") ? "bad" : /complete|succeed|done|merged/i.test(c.run_status ?? "") ? "ok" : /blocked/i.test(c.run_status ?? "") ? "you" : "wait"}
               name="Build" words={(c.run_status ?? "—").replace(/_/g, " ")} />
      ) : null}
      {suites.length ? suites.map((t) => (
        <Plain key={t.id || t.suite} tone={t.failed || t.ok === false ? "bad" : "ok"} name={suites.length > 1 ? `Tests · ${t.suite}` : "Tests"}
               words={t.failed ? `${t.failed} failed · ${t.passed} passed` : `${t.passed ? `${t.passed} passed` : "passed"}`} testid="ck-tests" />
      )) : tests != null || c?.run_status != null ? (
        <Plain tone={tests === "true" ? "ok" : tests === "false" ? "bad" : "wait"} name="Tests"
               words={tests === "true" ? "passed" : tests === "false" ? "failed" : tests ?? "not run yet"} testid="ck-tests" />
      ) : null}
      {(c?.items ?? []).map((i) => (
        <Plain key={`${i.app}${i.name}`} testid="gr-check-run" name={i.name}
               tone={i.conclusion === "success" || i.conclusion === "skipped" || i.conclusion === "neutral" ? "ok" : /fail|error|cancel|timed/.test(String(i.conclusion)) ? "bad" : "wait"}
               words={String(i.conclusion ?? i.status ?? "—")} />
      ))}
      {v?.guard || v?.error ? (
        <Plain tone="bad" testid="vr-guard" name={`${v.validator.id || "Validator"} · ${v.guard?.code || "error"}`}
               words={v.guard ? `The target equals ${v.guard.source || "another repo file"}, not the original file.` : v.error} />
      ) : null}
      {checkers.map((k) => <CheckerRow key={k.key} c={k} v={v} packet={packet} target={v?.target ?? ""} onJump={onJump}
                                       rechecking={rechecking && k.kind === "validator"} rulesHref={rulesHref} />)}
      {reviewOnly ? <Plain tone={packet!.counts.review!.blocking ? "bad" : "ok"} name="Reviewer"
                           words={`${packet!.counts.review!.found} found → ${packet!.counts.review!.open} open`} testid="ck-checker" /> : null}
      {cases.length ? <ContractRow cases={cases} target={v?.contract_target ?? ""} /> : null}
      {!c && !checkers.length && !suites.length ? <p className="cs-empty">No checks recorded for this change.</p> : null}
      {c?.where ? <p className="cs-faint cs-small ck-where">In {c.where}</p> : null}
    </div>
  );
}
