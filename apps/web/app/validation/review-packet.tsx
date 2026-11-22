"use client";

/**
 * THE REVIEW PACKET (SPEC §1.4–§1.5; wireframe C). Rendered on the merge card and on the change
 * page. Fixed order: verdict → severity chips → P0/P1 table → changed vs the partner's file →
 * checks and overrides → open risks → provenance. One row per finding (rule · line · fix), never a
 * sentence joined to the next; fact ids are a count chip, never inline.
 */
import React, { useState } from "react";
import { ArrowClockwise, ArrowSquareOut, GitDiff, Warning } from "@phosphor-icons/react";

import {
  countsWords, isGuardFinding, isReviewFinding, NOT_DEPLOYED, OVERRIDE_VERBS, packetStale, partnerNow, rerunPath, rerunPending, sendOverride, lineTitle, lineWords, packetTally,
  validationCounts, workRefsOf, reasonRows, refTarget, rootTitle, shortId, shortSha, SEVS, sortFindings,
  type Finding, type ReviewPacket, type Sev,
} from "../../lib/validation-review";
import { ActorName, KindBadge, RefChip, SevChip, Status } from "./kit";

export type PacketLinks = { run: string; build: string; change: string };

/** Console links for the packet's provenance line: the run by its wfr_ id, the build by its
 *  crun_ id — never one passed where the other belongs (SPEC §0 #8). */
export function packetLinks(projectId: string, p: ReviewPacket): PacketLinks {
  const q = (extra: Record<string, string>) => `/?${new URLSearchParams({ project_id: projectId, ...extra })}`;
  const refs = workRefsOf(p);
  return {
    run: refs.run ? q({ area: "delivery_workflows", wfr: refs.run }) : "",
    build: refs.build ? q({ area: "code_tasks", build: refs.build }) : "",
    change: p.work.change != null ? `/changes/${encodeURIComponent(projectId)}/${p.work.change}` : "",
  };
}

export function packetTitle(p: ReviewPacket, fallback = ""): string {
  const t = rootTitle(p.work.title || fallback);
  return p.work.change != null ? `Merge change #${p.work.change}${t ? ` · ${t}` : ""}` : t || "Merge this change";
}

/** Merge is disabled while the packet is invalid or a re-run after an override is pending. */
export function packetHold(p: ReviewPacket | null, pending = false, currentHead: string | null = null): { state: string; label: string; reason: string } | null {
  if (!p) return null;
  if (p.verdict.state === "invalid") {
    const n = p.verdict.reasons.length;
    return { state: "packet_invalid", label: "Approve and merge", reason: `Packet invalid: ${n} problem${n === 1 ? "" : "s"} · rebuild the packet first` };
  }
  if (pending || rerunPending(p)) return { state: "rerun_pending", label: "Re-checking…", reason: "Re-checking after your change" };
  if (packetStale(p, currentHead)) {
    return { state: "packet_stale", label: "Re-checking…", reason: `Stale: the checks ran on ${shortSha(p.head_sha) || "an older commit"}${currentHead ? `, the change is at ${shortSha(currentHead)}` : ""} · merge after the re-run` };
  }
  return null;
}

/** The rule (or seat) and where, as two parts: the rule on one clipped line, the place below it. */
function ruleParts(f: Finding): { rule: string; where: string } {
  if (f.source.kind === "validator" || !f.source.kind) return { rule: f.source.rule_id || f.id, where: lineWords(f) };
  // A guard on the partner's file: its line is the partner's, like every validator row (gap 11).
  if (isGuardFinding(f) && f.location.partner_line != null) return { rule: f.source.rule_id || f.source.name, where: lineWords(f) };
  const file = (f.location.file || "").split("/").pop() ?? "";
  return { rule: f.source.name || f.source.kind, where: file ? `${file}${f.location.line != null ? `:${f.location.line}` : ""}` : lineWords(f) };
}

function StatusCell({ f }: { f: Finding }) {
  if ((f.status === "open" || f.status === "reverted") && f.blocks) {
    // A security finding on a P0 rule's lines: never a follow-up, it blocks like the P0.
    return <Status st="open" label="open · blocks merge" />;
  }
  if (f.status === "open" && f.followup) {
    // Open but not blocking: BOSS moved it to a follow-up.
    const fu = f.followup;
    return <><Status st="deferred" label="deferred → follow-up" />{fu.change != null ? <> {fu.href
      ? <a href={fu.href} data-testid="vr-followup">#{fu.change}</a> : <span className="vr-mono">#{fu.change}</span>}</>
      : fu.build ? <> <span className="vr-mono vr-muted" title={fu.build}>{shortId(fu.build)}</span></> : null}</>;
  }
  if (f.status === "open" && f.override?.action === "waive" && !f.override.approved_at) {
    return <><Status st="pending" label="waive pending approval" /> <RefChip id={f.override.id} /></>;
  }
  return <><Status st={f.status} />{f.override ? <> <RefChip id={f.override.id} /></> : null}</>;
}

function FindingRow({ f }: { f: Finding }) {
  const kind = f.followup && !f.blocks ? "none" : f.override && (f.override.action === "edit" || f.override.action === "reject" || f.override.action === "remove") ? "human" : f.fix_kind;
  const { rule, where } = ruleParts(f);
  const reviewer = Boolean(f.source.kind && f.source.kind !== "validator");
  // Never "→ (removed)" for a line still on the branch (gap 7): partnerNow says what is true.
  const pn = partnerNow(f);
  const was = pn.before;
  const now = pn.after;
  // Long values are clipped to one line each; the whole value is the cell's tooltip.
  return (
    <tr data-sev={f.severity} id={refTarget(f.id)} data-testid="vr-packet-row" data-finding={f.id}>
      <td className="vr-mono vr-id vr-c-id">{f.id}</td>
      <td className="vr-mono vr-c-rule" title={[rule, lineTitle(f) || where].filter(Boolean).join(" · ")}>
        <span className="vr-clip">{rule}</span>
        {where || f.unmapped ? <span className="vr-where">{where}{f.unmapped ? <> <span className="vr-kind" title="No rule or standard named: counted as P2">unmapped</span></> : null}</span> : null}
      </td>
      <td className="vr-ba vr-c-ba" title={reviewer ? f.title || f.original || "" : `${was} → ${now}`}>
        {reviewer
          ? <span className="vr-muted vr-clip2">{f.title || f.original}</span>
          : <><span className="b vr-clip">{was}</span><span className="x vr-clip" data-kind={pn.kind} data-testid="vr-packet-now">→ {now}</span></>}
      </td>
      <td className="vr-c-fix">{f.followup && !f.blocks ? <span className="vr-kind">follow-up</span> : <KindBadge kind={kind} label={kind === "human" ? "you" : undefined} />}</td>
      <td className="vr-c-st"><StatusCell f={f} /></td>
    </tr>
  );
}

function SevBar({ p }: { p: ReviewPacket }) {
  // One count for every surface (packetTally): review follow-ups are their own cell, never P1.
  const t = packetTally(p);
  return (
    <div className="vr-sevbar" data-testid="vr-sevbar" data-review={t.review.found ? "" : undefined}>
      {SEVS.map((sev) => {
        const c = t[sev];
        const sub = c.found ? [`${c.found} found`, c.fixed ? `${c.fixed} fixed` : "", c.overridden ? `${c.overridden} overridden` : "", c.waived ? `${c.waived} waived` : ""]
          .filter(Boolean).join(" · ") : "—";
        return (
          <div key={sev} className="vr-sevcell" data-hot={(sev === "P0" || sev === "P1") && c.open ? "" : undefined} data-testid={`vr-sev-${sev}`}>
            <SevChip sev={sev} />
            <span className="num">{c.open}<span className="sub"> open</span></span>
            <span className="sub">{sub}</span>
          </div>
        );
      })}
      {t.review.found ? (
        <div className="vr-sevcell" data-testid="vr-sev-review" data-hot={t.review.blocking ? "" : undefined} title="Review seats' findings: counted apart from P0–P3; a follow-up does not block, a security finding on a P0 line does">
          <span className="vr-kind">review</span>
          <span className="num">{t.review.open}<span className="sub"> open</span></span>
          <span className="sub">{[`${t.review.found} found`, t.review.deferred ? `${t.review.deferred} follow-up${t.review.deferred === 1 ? "" : "s"}` : "",
            t.review.blocking ? `${t.review.blocking} blocking` : "not blocking"].filter(Boolean).join(" · ")}</span>
        </div>
      ) : null}
    </div>
  );
}

function Verdict({ p }: { p: ReviewPacket }) {
  if (p.verdict.state === "invalid") {
    const n = p.verdict.reasons.length;
    return <div className="vr-verdict" data-state="invalid" data-testid="vr-verdict"><b>Packet invalid</b><span className="vr-muted">{n} problem{n === 1 ? "" : "s"} · merge disabled</span></div>;
  }
  if (p.verdict.state === "blocked") {
    const t = packetTally(p);
    const open = [...(["P0", "P1"] as Sev[]).map((s) => (t[s].open ? `${t[s].open} ${s}` : "")),
                  t.review.blocking ? `${t.review.blocking} security review` : ""].filter(Boolean).join(" · ");
    return (
      <div className="vr-verdict" data-state="blocked" data-testid="vr-verdict"><b>{open ? "Blocked" : rerunPending(p) ? "Re-checking…" : "Blocked"}</b>
        <span className="vr-muted">{open ? `${open} open · merge waits on them` : rerunPending(p) ? "after your change · merge waits for the re-run"
          : p.verdict.reasons.includes("rerun_failed") ? "the re-run failed · rebuild the packet"
          : p.verdict.reasons.includes("stale_evidence") ? "the checks ran on an older commit · re-checking at the head"
          : p.verdict.reasons.some((r) => r.startsWith("unexplained_hunk")) ? "an unexplained change in the partner's file" : "not ready"}</span>
        {p.verdict.reasons.map((r) => { const id = r.split(":").pop() ?? r; return /^[A-Z]-\d+$/.test(id) ? <RefChip key={r} id={id} />
          : r === "rerun_pending" ? <span key={r} className="vr-st" data-st="pending">re-checking…</span> : null; })}
      </div>
    );
  }
  return <div className="vr-verdict" data-state="ready" data-testid="vr-verdict"><b>Ready</b><span className="vr-muted">No P0 or P1 open · every claim has evidence</span></div>;
}

export function InvalidProblems({ p }: { p: ReviewPacket }) {
  const rows = reasonRows(p.verdict.reasons);
  return (
    <>
      <div className="vr-scroll"><table data-testid="vr-invalid">
        <thead><tr><th>Check</th><th>Problem</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.check + r.problem} data-sev="P0"><td className="vr-mono vr-id">{r.check}</td><td>{r.problem || "—"}</td></tr>)}</tbody>
      </table></div>
      <p className="vr-note">Built only from typed step outputs; nothing falls back to prose. A claim must cite a finding, evidence or override id.</p>
    </>
  );
}

function ChangedVsPartner({ p }: { p: ReviewPacket }) {
  const v = p.vs_input;
  if (!v.file && !v.hunks) return null;
  return (
    <div data-testid="vr-vs-input">
      <div className="vr-k">Changed vs the partner&apos;s file</div>
      <div className="vr-row vr-small">
        <GitDiff size={14} aria-hidden /><span className="vr-mono">{v.file}</span>
        <span className="vr-mono vr-muted">{v.hunks} hunks · <span style={{ color: "var(--v2-green)" }}>+{v.added}</span> <span style={{ color: "var(--v2-red)" }}>−{v.removed}</span></span>
      </div>
      <div className="vr-chips" style={{ marginTop: 6 }}>
        {Object.entries(v.hunks_by_finding).map(([h, ids]) => (
          <span key={h} className="vr-pill">{h} → {ids.map((id) => <RefChip key={id} id={id} />)}</span>
        ))}
        {v.unexplained_hunks.map((h) => <span key={h} className="vr-pill vr-unexplained" data-testid="vr-unexplained"><Warning size={11} aria-hidden />{h} unexplained change</span>)}
      </div>
    </div>
  );
}

function Checks({ p }: { p: ReviewPacket }) {
  const contractPass = p.contract.filter((c) => c.got != null && String(c.got) === String(c.expect)).length;
  // THE SAME COUNT the Work panel's funnel shows (validationCounts), guard findings included; a
  // thin packet with no findings keeps the engine's own row.
  const counted = p.findings.some((f) => !isReviewFinding(f));
  const rowOf = (id: string, i: number) => {
    const mine = p.findings.filter((f) => !isReviewFinding(f) && ((!isGuardFinding(f) && (f.source.name === id || p.validators.length === 1)) || (isGuardFinding(f) && i === 0)));
    return validationCounts(mine, p.validators[i]?.before ?? 0);
  };
  if (!p.validators.length && !p.tests.length && !p.contract.length) return null;
  return (
    <div>
      <div className="vr-k">Checks</div>
      <table><tbody>
        {p.validators.map((v, i) => {
          const c = counted ? rowOf(v.id, i) : null;
          const open = c ? c.open : v.after_open;
          const waived = c ? c.waived : v.after_waived;
          return (
            <tr key={v.id} id={v.evidence ? refTarget(v.evidence) : undefined} data-testid="vr-check-validator">
              <td className="vr-mono vr-id">{v.id}</td>
              <td className="vr-mono" style={{ color: open ? "var(--v2-red)" : "var(--v2-green)" }}>{c ? countsWords(c) : `${v.before} → ${v.after_open} open`}{waived ? ` · ${waived} waived` : ""}</td>
              <td>{v.evidence ? <RefChip id={v.evidence} /> : null}</td>
            </tr>
          );
        })}
        {p.tests.map((t) => (
          <tr key={t.id} id={refTarget(t.id)}>
            <td className="vr-mono vr-id">{t.suite}</td>
            <td className="vr-mono" style={{ color: t.failed || t.ok === false ? "var(--v2-red)" : "var(--v2-green)" }}>{t.failed ? `${t.failed} failing` : t.ok === false ? "failing" : t.passed ? `${t.passed} pass` : "pass"}</td>
            <td>{t.evidence ? <RefChip id={t.evidence} /> : null}</td>
          </tr>
        ))}
        {p.contract.length ? (
          <tr>
            <td className="vr-mono vr-id">contract</td>
            <td className="vr-mono" style={{ color: contractPass === p.contract.length ? "var(--v2-green)" : "var(--v2-red)" }}>{contractPass} / {p.contract.length}</td>
            <td><span className="vr-chips">{p.contract.map((c) => <span key={c.id} id={refTarget(c.id)} className="vr-mono vr-tiny vr-muted" title={`${c.case}: expect ${c.expect}, got ${c.got}`}>{c.id}</span>)}</span></td>
          </tr>
        ) : null}
      </tbody></table>
    </div>
  );
}

function Overrides({ p }: { p: ReviewPacket }) {
  if (!p.overrides.length) return null;
  return (
    <div>
      <div className="vr-k">Overrides ({p.overrides.length})</div>
      <table><tbody>
        {p.overrides.map((r) => (
          <tr key={r.id} id={refTarget(r.id)} data-testid="vr-override-row">
            <td className="vr-mono vr-id">{r.id}</td>
            <td className="vr-small" data-action={r.action}><ActorName id={r.actor} name={r.actor_name} /> {OVERRIDE_VERBS[r.action]}{r.finding_id ? ` ${r.finding_id}` : ""}{(r.action === "waive" || r.action === "remove") && r.reason ? ` · “${r.reason}”` : ""}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

export function ReviewPacketView({ packet: p, projectId, fallbackTitle = "", eyebrow = "Control plane proposes · merge", actions, testid = "review-packet" }: {
  packet: ReviewPacket; projectId: string; fallbackTitle?: string; eyebrow?: string; actions?: React.ReactNode; testid?: string;
}) {
  const links = packetLinks(projectId, p);
  const rows = sortFindings(p.findings);
  const top = rows.filter((f) => f.severity === "P0" || f.severity === "P1");
  const rest = rows.filter((f) => f.severity === "P2" || f.severity === "P3");
  const invalid = p.verdict.state === "invalid";
  return (
    <section className="vr vr-card vr-packet" data-testid={testid} data-verdict={p.verdict.state} aria-label="Review packet">
      <div className="vr-row"><span className="vr-mono vr-dim vr-small">{eyebrow}</span><span className="vr-grow" />
        <span className="vr-mono vr-dim vr-small">{[p.packet_version != null ? `packet v${p.packet_version}` : "", p.head_sha ? `head ${shortSha(p.head_sha)}` : ""].filter(Boolean).join(" · ")}</span></div>
      <h2 data-testid={`${testid}-title`}>{packetTitle(p, fallbackTitle)}</h2>
      <Verdict p={p} />
      {invalid ? <InvalidProblems p={p} /> : (
        <>
          <SevBar p={p} />
          {top.length ? (
            <div>
              <div className="vr-k">P0 / P1 · what changed vs the partner&apos;s file</div>
              <div className="vr-scroll"><table className="vr-ftable" data-testid="vr-p01">
                <thead><tr><th className="vr-c-id">ID</th><th className="vr-c-rule">Rule · line</th><th className="vr-c-ba">Partner → now</th><th className="vr-c-fix">Fix</th><th className="vr-c-st">Status</th></tr></thead>
                <tbody>{top.map((f) => <FindingRow key={f.id} f={f} />)}</tbody>
              </table></div>
            </div>
          ) : <div className="vr-empty" data-testid="vr-no-p01">No P0 or P1 findings in this packet.</div>}
          {rest.length ? (
            <details data-testid="vr-p23">
              <summary className="vr-small vr-muted" style={{ cursor: "pointer" }}>P2 / P3 · {rest.length} ({rest.filter((f) => f.status === "open").length} open)</summary>
              <div className="vr-scroll"><table className="vr-ftable"><tbody>{rest.map((f) => <FindingRow key={f.id} f={f} />)}</tbody></table></div>
            </details>
          ) : null}
          <ChangedVsPartner p={p} />
          <div className="vr-two"><Checks p={p} /><Overrides p={p} /></div>
          {p.risks.length ? (
            <div data-testid="vr-risks">
              <div className="vr-k">Open risks</div>
              {p.risks.map((r) => <div key={r.text} className="vr-risk">{r.text} {r.refs.map((id) => <RefChip key={id} id={id} />)}</div>)}
            </div>
          ) : null}
        </>
      )}
      <div className="vr-prov" data-testid="vr-provenance">
        {links.run ? <a href={links.run} data-testid="vr-prov-run"><ArrowSquareOut size={11} aria-hidden /> Run{p.work.run_no != null ? ` #${p.work.run_no}` : ""}</a> : null}
        {links.build ? <a href={links.build} data-testid="vr-prov-build"><ArrowSquareOut size={11} aria-hidden /> Build {shortId(p.work.build)}</a> : null}
        {links.change ? <a href={links.change} data-testid="vr-prov-change"><ArrowSquareOut size={11} aria-hidden /> Change #{p.work.change}</a> : null}
        {p.ruleset.id ? <span>ruleset {p.ruleset.id}{p.ruleset.version != null ? ` v${p.ruleset.version}` : ""}{p.ruleset.sha ? ` · ${shortSha(p.ruleset.sha)}` : ""}</span> : null}
        {p.ruleset.stale ? <span className="vr-unexplained">ruleset changed since check · re-run</span> : null}
        {p.facts ? <span className="vr-pill" data-testid="vr-facts" title="Knowledge facts this decision used">{p.facts} facts</span> : null}
      </div>
      {actions}
    </section>
  );
}

/** An invalid packet's one way forward (SPEC §1.4 rule 6): rebuild it from the step outputs. A
 *  BOSS without the route shows the button disabled, saying so. */
export function RebuildPacket({ projectId, change }: { projectId: string; change: number | null }) {
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  if (change == null) return null;
  const go = async () => {
    setBusy(true);
    const r = await sendOverride(rerunPath(projectId, change), { reason: "rebuild packet" });
    setBusy(false);
    if (r.missing) { setMissing(true); setSaid({ text: NOT_DEPLOYED, tone: "amber" }); return; }
    setSaid(r.ok ? { text: "Rebuilding the packet from the step outputs.", tone: "" } : { text: r.error ?? "It did not start.", tone: "red" });
  };
  return (
    <div className="vr-row" data-testid="vr-rebuild">
      <button type="button" className="vr-btn" data-kind="mint" disabled={busy || missing} title={missing ? NOT_DEPLOYED : "Rebuild the packet from the step outputs"}
              onClick={() => void go()} data-testid="vr-rebuild-btn"><ArrowClockwise size={13} aria-hidden />{busy ? "Rebuilding…" : "Rebuild packet"}</button>
      {said ? <span className="vr-said" data-tone={said.tone || undefined} role="status">{said.text}</span> : null}
    </div>
  );
}
