"use client";

/**
 * THE WORK PANEL'S BUILT COLUMN FOR VALIDATION WORK (SPEC §5.3; wireframe A): the validator's
 * funnel, never the coder's prose — `gwlint · 9 found → 4 auto · 3 agent · 1 you · 1 waived →
 * 0 open`, the P0/P1 chips, contract and tests, later builds as lines, and "See every fix".
 */
import React from "react";
import { ArrowRight } from "@phosphor-icons/react";

import { countsOf, funnelOf, type Sev, type Validation } from "../../lib/validation-review";
import { SevChip } from "./kit";

export function ValidationBuilt({ v, tests, later, fixesHref, testid }: {
  v: Validation;
  tests: { passed: boolean | null; count: number | null; label: string } | null;
  later: { key: string; line: React.ReactNode }[];
  fixesHref: string; testid: string;
}) {
  const f = funnelOf(v);
  const c = countsOf(v.findings);
  const pass = v.contract.filter((k) => k.got != null && String(k.got) === String(k.expect)).length;
  return (
    <div className="vr" data-testid={`${testid}-validation`}>
      <div className="vr-funnel" data-testid={`${testid}-funnel`} aria-label={`${v.validator.id}: ${f.found} found, ${f.auto} auto-fixed, ${f.agent} by an agent, ${f.human} by you, ${f.waived} waived, ${f.open} open`}>
        <span>{v.validator.id || "validator"}</span><span className="n" data-tone="red">{f.found}</span><span className="vr-muted">found</span>
        <span className="arrow">→</span>
        {f.auto ? <span className="vr-kind" data-kind="auto">{f.auto} auto</span> : null}
        {f.agent ? <span className="vr-kind" data-kind="agent">{f.agent} agent</span> : null}
        {f.human ? <span className="vr-kind" data-kind="human">{f.human} you</span> : null}
        {f.waived ? <span className="vr-kind">{f.waived} waived</span> : null}
        <span className="arrow">→</span><span className="n" data-tone={f.open ? "red" : "green"}>{f.open}</span><span className="vr-muted">open</span>
      </div>
      <div className="vr-chips" style={{ marginBottom: 10 }} data-testid={`${testid}-sev`}>
        {(["P0", "P1", "P2", "P3"] as Sev[]).filter((s) => c[s].found).map((s) => (
          <span key={s} className="vr-chip-pair"><SevChip sev={s} /><span className="vr-mono vr-small">{s === "P2" || s === "P3"
            ? [c[s].waived ? `${c[s].waived} waived` : "", c[s].open ? `${c[s].open} open` : "", !c[s].waived && !c[s].open ? `${c[s].found} found` : ""].filter(Boolean).join(" · ")
            : `${c[s].found} found · ${c[s].open} open`}</span></span>
        ))}
      </div>
      <table className="vr-facts"><tbody>
        {v.contract.length ? <tr><td>Contract</td><td className="vr-mono" style={{ color: pass === v.contract.length ? "var(--v2-green)" : "var(--v2-red)" }}>{pass} / {v.contract.length} pass</td></tr> : null}
        {tests ? <tr><td>Tests</td><td className="vr-mono" style={{ color: tests.passed === false ? "var(--v2-red)" : tests.passed ? "var(--v2-green)" : undefined }}>{tests.label}</td></tr> : null}
        {later.length ? <tr><td>Later builds</td><td>{later.map((l, i) => <React.Fragment key={l.key}>{i ? " · " : ""}{l.line}</React.Fragment>)}</td></tr> : null}
      </tbody></table>
      {fixesHref ? <a href={fixesHref} className="vr-small" style={{ display: "inline-flex", gap: 4, alignItems: "center", marginTop: 8 }} data-testid={`${testid}-see-fixes`}>See every fix <ArrowRight size={12} aria-hidden /></a> : null}
    </div>
  );
}
