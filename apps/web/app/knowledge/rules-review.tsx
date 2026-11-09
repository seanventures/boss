"use client";

/**
 * RULES REVIEW (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.7): the business rules agents proposed, each
 * with the code that shows it, for a person to Approve, Edit (a new version, approved in the same
 * act) or Reject. A data-quality rule read from a rulebook (rulebook source G4/G5) is reviewed
 * here too: its card shows the rule id, what it checks, its Warning and Error thresholds, the data
 * it protects and the clause it was read from (quoted, with its page); approving it approves its
 * thresholds. Filters by status, scan and area; bulk approve with a count and a confirm; the
 * actions are pre-greyed for a viewer without rule review (GatedButton) and disabled, with the
 * reason, on an archived project. The Facts sub-tab holds today's fact promotion queue (moved
 * here from Approvals, §5.1).
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import { LockedPanel } from "../../components/locked-panel";
import { KgReadError } from "./kg-states";
import { agentName, personName } from "../../lib/kg-provenance";
import { splitWhen, wordCut } from "../../lib/word-cut";
import {
  FACT_MANAGE, RULE_REVIEW, approveRule, codeHref, editRule, getGraphStatus, getRules, kgRunHref, promoteFact,
  rejectFact, rejectRule,
  kgWhen,
} from "../../lib/kg-view";
import { KG_RULE_CATEGORIES, type KgClauseEvidence, type KgDqRule, type KgDqThreshold, type KgRule, type KgRuleCategory, type KgRulesResponse, type KgRuleStatus } from "../../lib/kg-view-types";

type StatusFilter = KgRuleStatus | "all";
const STATUS: Array<{ key: StatusFilter; label: string }> = [
  { key: "proposed", label: "Proposed" }, { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" }, { key: "all", label: "All" },
];

/** One formatter for the area: the viewer's own zone, labelled (lib/kg-view.ts kgWhen). */
const when = kgWhen;

export function RulesReview({ projectId, actor, orgId, archived, locked, initialScan, initialSub, onFocusNode, editId, onCount }: {
  projectId: string; actor: string; orgId?: string | null;
  archived?: boolean;
  /** The add-on port refused Rules (§3.7): the locked state, never a broken button. */
  locked?: string | null;
  initialScan?: string | null;
  initialSub?: "rules" | "facts";
  onFocusNode: (id: string) => void;
  /** Open this rule's edit form (from the detail panel's Edit). */
  editId?: string | null;
  onCount?: (proposed: number) => void;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [sub, setSub] = useState<"rules" | "facts">(initialSub ?? "rules");
  const [status, setStatus] = useState<StatusFilter>("proposed");
  const [scan, setScan] = useState<string>(initialScan ?? "");
  const [area, setArea] = useState<string>("");
  const [data, setData] = useState<KgRulesResponse | null>(null);
  const [error, setError] = useState<{ status: number; text: string; unavailable?: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [confirmBulk, setConfirmBulk] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => { if (initialScan !== undefined) setScan(initialScan ?? ""); }, [initialScan]);
  useEffect(() => { if (initialSub) setSub(initialSub); }, [initialSub]);
  useEffect(() => { if (editId) { setSub("rules"); setStatus("all"); } }, [editId]);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await getRules(w, { status: status === "all" ? null : status, scan_id: scan || null, area: area || null });
    setLoading(false);
    if (r.ok) { setData(r.data); setError(null); onCount?.(r.data.counts.proposed ?? 0); }
    else setError({ status: r.status, text: r.error, unavailable: r.unavailable });
  }, [w, status, scan, area, onCount]);

  useEffect(() => { if (!locked && sub === "rules") void load(); }, [load, locked, sub, nonce]);
  useEffect(() => setPicked(new Set()), [status, scan, area]);

  if (locked) {
    return (
      <div className="kg-review" data-testid="kg-rules-locked">
        <div className="kg-stage kg-stage-short">
          <div className="kg-empty">
            <h3 className="kg-empty-title">Rules and Learning are an add-on</h3>
            <LockedPanel label="Locked" reason={locked} testId="kg-locked" />
            <p className="kg-empty-body">The code graph stays fully available. Rules agents propose and what they learn while they work need the Knowledge add-on.</p>
          </div>
        </div>
      </div>
    );
  }

  const items = data?.items ?? [];
  const scans = [...new Set([...(data?.scans ?? []).map((x) => x.id),
    ...(data?.items ?? []).map((r) => r.scan_id).filter((s): s is string => !!s), ...(scan ? [scan] : [])])];
  // A scan by what it read and when (the server's words), never its raw id.
  // A run's title is cut at a word (round 6), the whole of it the option's tooltip.
  const scanLabel = (id: string) => {
    const known = data?.scans?.find((x) => x.id === id);
    if (!known) return { text: "A scan", full: "A scan" };
    const n = ` (${known.rules} rule${known.rules === 1 ? "" : "s"})`;
    const [title, at] = splitWhen(known.label);
    const cut = wordCut(title, 72);
    return { text: `${cut.text}${at}${n}`, full: `${cut.full}${at}${n}` };
  };
  const areas = [...new Set((data?.items ?? []).flatMap((r) => r.scope.areas))].sort();
  const proposedShown = items.filter((r) => r.status === "proposed");
  const archivedWhy = "This project is archived: nothing new runs on it. Restore it to review rules.";
  const done = (text: string) => { setToast({ ok: true, text }); setNonce((n) => n + 1); };

  const bulk = async () => {
    setBulkBusy(true);
    const ids = [...picked];
    let ok = 0;
    let fail = "";
    for (const id of ids) {
      const r = await approveRule(w, id);
      if (r.ok) ok += 1; else fail = r.error;
    }
    setBulkBusy(false);
    setConfirmBulk(false);
    setPicked(new Set());
    if (fail) setToast({ ok: false, text: `${ok} of ${ids.length} approved. The rest were refused: ${fail}` });
    else done(`${ok} ${ok === 1 ? "rule" : "rules"} approved. Agents rely on them from now on.`);
    setNonce((n) => n + 1);
  };

  return (
    <div className="kg-review" data-testid="kg-rules-review">
      <div className="kg-review-bar">
        <div className="inf-seg" role="tablist" aria-label="Review">
          <button type="button" role="tab" className="inf-seg-btn" aria-selected={sub === "rules"} data-testid="kg-review-sub-rules" onClick={() => setSub("rules")}>Rules</button>
          <button type="button" role="tab" className="inf-seg-btn" aria-selected={sub === "facts"} data-testid="kg-review-sub-facts" onClick={() => setSub("facts")}>Facts</button>
        </div>
        {sub === "rules" ? (
          <>
            <div className="inf-seg" role="radiogroup" aria-label="Status">
              {STATUS.map((s) => (
                <button key={s.key} type="button" role="radio" className="inf-seg-btn" aria-checked={status === s.key}
                        data-testid={`kg-review-status-${s.key}`} onClick={() => setStatus(s.key)}>
                  {s.label}{s.key !== "all" && data ? <span className="inf-tab-count"> {data.counts[s.key as KgRuleStatus] ?? 0}</span> : null}
                </button>
              ))}
            </div>
            <label className="kg-select">
              <span>Scan</span>
              <select value={scan} onChange={(e) => setScan(e.target.value)} data-testid="kg-review-scan" aria-label="Filter by scan">
                <option value="">Every scan</option>
                {scans.map((s) => { const l = scanLabel(s); return <option key={s} value={s} title={l.full}>{l.text}</option>; })}
              </select>
            </label>
            <label className="kg-select">
              <span>Area</span>
              <select value={area} onChange={(e) => setArea(e.target.value)} data-testid="kg-review-area" aria-label="Filter by area">
                <option value="">Every area</option>
                {areas.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <span className="ag-spacer" />
            {picked.size ? (
              confirmBulk ? (
                <span className="kg-confirm" role="alertdialog" aria-label={`Approve ${picked.size} rules?`} data-testid="kg-bulk-confirm">
                  <span>Approve {picked.size} {picked.size === 1 ? "rule" : "rules"}? Every agent relies on them from then on.</span>
                  <GatedButton permission={RULE_REVIEW} className="ag-btn ag-btn-primary" data-testid="kg-bulk-yes" disabled={bulkBusy || archived}
                               title={archived ? archivedWhy : undefined} onClick={() => void bulk()}>{bulkBusy ? "Approving…" : "Approve"}</GatedButton>
                  <button type="button" className="inf-link" onClick={() => setConfirmBulk(false)}>Cancel</button>
                </span>
              ) : (
                <GatedButton permission={RULE_REVIEW} className="ag-btn ag-btn-primary" data-testid="kg-bulk" disabled={archived}
                             title={archived ? archivedWhy : undefined} onClick={() => setConfirmBulk(true)}>Approve {picked.size} selected</GatedButton>
              )
            ) : null}
          </>
        ) : null}
      </div>
      {archived ? <p className="kg-banner" data-tone="amber" role="note" data-testid="kg-archived">This project is archived. You can read its rules; reviewing them is off until it is restored.</p> : null}
      {toast ? <p className={toast.ok ? "kg-ok" : "kg-inline-err"} role={toast.ok ? "status" : "alert"} data-testid="kg-review-toast">{toast.text}</p> : null}

      {sub === "facts" ? <FactsQueue w={w} /> : (
        <>
          {error ? <KgReadError error={error} onRetry={() => void load()} testId="kg-review-error" what="Rules review" /> : null}
          {loading && !data ? (
            <ul className="kg-cards" aria-busy="true" data-testid="kg-review-loading">
              {Array.from({ length: 3 }, (_, i) => <li key={i} className="kg-card kg-card-skel"><span className="kg-skel-row" /><span className="kg-skel-row" /><span className="kg-skel-row" style={{ width: "40%" }} /></li>)}
            </ul>
          ) : null}
          {data && !items.length ? (
            <div className="kg-stage kg-stage-short" data-testid="kg-review-empty">
              <div className="kg-empty">
                <h3 className="kg-empty-title">{status === "proposed" ? "Nothing waiting for review" : `No ${status === "all" ? "" : `${status} `}rules${scan ? " from this scan" : ""}`}</h3>
                <p className="kg-empty-body">
                  Agents propose business rules when they scan an area: pick an area or package in the graph and press
                  <strong> Scan for rules here</strong>. Each proposal arrives here with the lines of code that show it.
                  A rulebook uploaded in <strong>Documents</strong> brings its rules here too, after <strong>Extract rules</strong>.
                </p>
              </div>
            </div>
          ) : null}
          {items.length ? (
            <ul className="kg-cards" data-testid="kg-review-list">
              {items.map((r) => (
                <RuleCard key={r.node.id} r={r} w={w} orgId={orgId} archived={!!archived} archivedWhy={archivedWhy}
                          picked={picked.has(r.node.id)} editing={editId === r.node.id}
                          onPick={(on) => setPicked((s) => { const n = new Set(s); if (on) n.add(r.node.id); else n.delete(r.node.id); return n; })}
                          onFocusNode={onFocusNode} onDone={done} onFail={(t) => setToast({ ok: false, text: t })} />
              ))}
            </ul>
          ) : null}
          {proposedShown.length > 1 && !picked.size ? (
            <p className="kg-quiet">Tick rules to approve several at once.</p>
          ) : null}
        </>
      )}
    </div>
  );
}

/** "0.05%" for a rate, "USD 1,000" for money, "30 min" for lateness, "5" for a count. */
export function thresholdValue(t: Pick<KgDqThreshold, "metric" | "value">): string | null {
  if (t.value === null || t.value === undefined) return null;
  const v = Number(t.value);
  if (t.metric === "rate" || t.metric === "relative_diff") return `${+(v * 100).toPrecision(6)}%`;
  if (t.metric === "amount_usd") return `USD ${v.toLocaleString("en-US")}`;
  if (t.metric === "minutes") return `${v} min`;
  return String(v);
}

const METRIC_WORDS: Record<string, string> = {
  rate: "failure rate", relative_diff: "relative difference", amount_usd: "amount difference",
  minutes: "lateness", count: "failure count",
};

/** Where a rule was read from: the document, the clause, its page, and the passage quoted. */
function ClauseChips({ items, onFocusNode }: { items: KgClauseEvidence[]; onFocusNode: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <ul className="kg-evidence" aria-label="Read from">
      {items.map((e) => (
        <li key={e.clause_id} className="kg-ev" data-testid="kg-rule-clause">
          <button type="button" className="kg-ev-chip" onClick={() => onFocusNode(e.clause_id)} data-testid="kg-clause-focus"
                  title={e.citation ?? e.section}>
            <span className="kg-dot" data-layer="domain" aria-hidden />
            <span>{e.document ? <span data-user-text>{e.document}</span> : "Document"}, {e.section}{e.page ? `, p. ${e.page}` : ""}</span>
          </button>
          {e.quote ? <blockquote className="kg-ev-quote" data-user-text>{e.quote}</blockquote> : null}
        </li>
      ))}
    </ul>
  );
}

function linkWords(dq: KgDqRule): { linked: string; unmatched: string } {
  const words: Record<string, [string, string]> = {
    cde: ["CDE", "CDEs"], column: ["column", "columns"], table: ["table", "tables"], report_line: ["report line", "report lines"],
  };
  const say = (m?: Record<string, string[]>) => Object.entries(m ?? {})
    .filter(([, v]) => v.length)
    .map(([k, v]) => `${v.length} ${(words[k] ?? [k, `${k}s`])[v.length === 1 ? 0 : 1]}`).join(", ");
  return { linked: say(dq.links?.linked), unmatched: say(dq.links?.unmatched) };
}

/** A data-quality rule's rulebook fields: what it checks, when it warns or errors, what it protects. */
function DqDetails({ dq }: { dq: KgDqRule }) {
  const links = linkWords(dq);
  return (
    <div className="kg-dq" data-testid="kg-dq-details">
      {dq.check ? <p className="kg-dq-check"><span className="kg-quiet">Check</span> <span data-user-text>{dq.check}</span></p> : null}
      {dq.thresholds.length ? (
        <ul className="kg-dq-thresholds" aria-label="Thresholds">
          {dq.thresholds.map((t) => {
            const value = thresholdValue(t);
            return (
              <li key={t.level} className="kg-dq-threshold" data-level={t.level} data-testid="kg-dq-threshold">
                <span className="kg-dq-level">{t.level === "error" ? "Error when" : "Warning when"}</span>
                <span data-user-text>{t.text}</span>
                {value ? <span className="kg-chip" title={t.metric ? METRIC_WORDS[t.metric] ?? t.metric : undefined}>{t.metric ? `${METRIC_WORDS[t.metric] ?? t.metric} > ${value}` : value}</span> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      <dl className="kg-dq-facts">
        {dq.cdes.length ? <><dt>Protects</dt><dd>{dq.cdes.map((c) => <span key={c} className="kg-chip">{c}</span>)}</dd></> : null}
        {dq.columns.length ? <><dt>Applies to</dt><dd className="inf-mono" data-user-text>{dq.columns.join(", ")}</dd></> : null}
        {dq.report_lines.length ? <><dt>Reaches</dt><dd data-user-text>{dq.report_lines.join("; ")}</dd></> : null}
        {dq.legal_reference ? <><dt>Law</dt><dd data-user-text>{dq.legal_reference}</dd></> : null}
        {dq.owner ? <><dt>Owner</dt><dd data-user-text>{dq.owner}</dd></> : null}
        {dq.related.length ? <><dt>Related</dt><dd>{dq.related.join(", ")}</dd></> : null}
      </dl>
      {links.linked || links.unmatched ? (
        <p className="kg-quiet" data-testid="kg-dq-links">
          {links.linked ? <>Linked in the graph: {links.linked}.</> : null}
          {links.unmatched ? <> Not in the graph yet: {links.unmatched}.</> : null}
        </p>
      ) : null}
    </div>
  );
}

function RuleCard({ r, w, orgId, archived, archivedWhy, picked, editing, onPick, onFocusNode, onDone, onFail }: {
  r: KgRule; w: { projectId: string; actor: string }; orgId?: string | null; archived: boolean; archivedWhy: string;
  picked: boolean; editing: boolean;
  onPick: (on: boolean) => void; onFocusNode: (id: string) => void; onDone: (text: string) => void; onFail: (text: string) => void;
}) {
  const [mode, setMode] = useState<"view" | "edit" | "reject">(editing ? "edit" : "view");
  const [busy, setBusy] = useState<string | null>(null);
  const [title, setTitle] = useState(r.title);
  const [statement, setStatement] = useState(r.statement);
  const [category, setCategory] = useState<KgRuleCategory>(r.category);
  const [areas, setAreas] = useState(r.scope.areas.join(", "));
  const [packages, setPackages] = useState(r.scope.packages.join(", "));
  const [note, setNote] = useState("");
  useEffect(() => { if (editing) setMode("edit"); }, [editing]);
  const run = kgRunHref(r.provenance, orgId);
  const proposed = r.status === "proposed";
  const by = agentName(r.provenance) ?? personName(r.provenance.created_by, null);
  const go = async (label: string, fn: () => Promise<{ ok: boolean; error?: string }>, ok: string) => {
    setBusy(label);
    const res = await fn();
    setBusy(null);
    if (res.ok) { setMode("view"); onDone(ok); } else onFail(res.error ?? "It did not go through.");
  };
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const dq = r.kind === "data_quality_rule" ? r.dq ?? null : null;
  return (
    <li className="kg-card" data-testid="kg-rule-card" data-status={r.status} data-rule={r.node.id} data-kind={r.kind ?? "rule"}>
      <div className="kg-card-head">
        {proposed ? (
          <input type="checkbox" className="kg-check" checked={picked} onChange={(e) => onPick(e.target.checked)}
                 aria-label={`Select “${r.title}”`} data-testid="kg-rule-pick" disabled={archived} />
        ) : null}
        <span className="kg-dot" style={{ background: dq ? "var(--kg-layer-domain)" : "var(--kg-rule)" }} aria-hidden />
        {dq ? <span className="kg-chip kg-dq-id" data-testid="kg-dq-id">{dq.rule_id}{dq.version ? ` v${dq.version}` : ""}</span> : null}
        <h4 className="kg-card-title" data-user-text>{r.title}</h4>
        <span className="kg-status" data-status={r.status}>{r.status}</span>
      </div>
      {mode === "edit" ? (
        <form className="kg-edit" data-testid="kg-rule-edit" noValidate onSubmit={(e) => {
          e.preventDefault();
          if (!statement.trim() || !title.trim()) return;
          void go("edit", () => editRule(w, r.node.id, dq
            ? { title: title.trim(), statement: statement.trim() }
            : { title: title.trim(), statement: statement.trim(), category, scope: { areas: list(areas), packages: list(packages) } }),
          `Saved as version ${r.version + 1} and approved.`);
        }}>
          <label className="kg-field"><span>Title</span><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" data-testid="kg-edit-title" /></label>
          <label className="kg-field"><span>Statement</span><textarea className="input" rows={3} value={statement} onChange={(e) => setStatement(e.target.value)} aria-label="Statement" data-testid="kg-edit-statement" /></label>
          {dq ? null : <><div className="kg-edit-row">
            <label className="kg-field"><span>Category</span>
              <select value={category} onChange={(e) => setCategory(e.target.value as KgRuleCategory)} aria-label="Category" data-testid="kg-edit-category">
                {KG_RULE_CATEGORIES.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
              </select>
            </label>
            <label className="kg-field"><span>Areas</span><input className="input" value={areas} onChange={(e) => setAreas(e.target.value)} aria-label="Areas, comma separated" /></label>
          </div>
          <label className="kg-field"><span>Packages</span><input className="input" value={packages} onChange={(e) => setPackages(e.target.value)} aria-label="Packages, comma separated" /></label></>}
          {!statement.trim() || !title.trim() ? <p className="kg-inline-err" role="alert">A rule needs a title and a statement.</p> : null}
          <p className="kg-quiet">Saving makes version {r.version + 1}, which supersedes this one and is approved in the same act.</p>
          <div className="kg-panel-actions">
            <GatedButton permission={RULE_REVIEW} type="submit" className="ag-btn ag-btn-primary" data-testid="kg-edit-save"
                         disabled={busy !== null || archived || !statement.trim() || !title.trim()} title={archived ? archivedWhy : undefined}>
              {busy === "edit" ? "Saving…" : "Save and approve"}
            </GatedButton>
            <button type="button" className="inf-link" onClick={() => setMode("view")}>Cancel</button>
          </div>
        </form>
      ) : (
        <>
          <p className="kg-card-statement" data-user-text>{r.statement}</p>
          {r.rationale ? <p className="kg-card-rationale"><span className="kg-quiet">Why:</span> <span data-user-text>{r.rationale}</span></p> : null}
          {dq ? <DqDetails dq={dq} /> : null}
        </>
      )}
      <div className="kg-card-meta">
        {dq ? <>
          {dq.dimension ? <span className="kg-chip" data-testid="kg-dq-dimension">{dq.dimension}</span> : null}
          {dq.severity ? <span className="kg-chip" data-severity={dq.severity.toLowerCase()}>{dq.severity} severity</span> : null}
          {dq.system ? <span>{dq.system}{dq.feed ? `, ${dq.feed}` : ""}</span> : null}
        </> : <span className="kg-chip">{r.category.replace(/_/g, " ")}</span>}
        <span title="How sure the proposing agent was, capped by its evidence">{Math.round(r.confidence * 100)}% confident</span>
        <span>Proposed by <strong>{by}</strong>{r.provenance.at ? `, ${when(r.provenance.at)}` : ""}</span>
        {run ? <a className="inf-link" href={run} data-testid="kg-rule-run">its run ↗</a> : null}
        {/* Names the approver (round 6): the person's display name, else "the operator". */}
        {r.review ? <span data-testid="kg-rule-approver">{r.status === "rejected" ? "Rejected" : r.status === "approved" ? "Approved" : "Reviewed"} by <strong>{personName(r.review.by, r.review.by_name)}</strong>, {when(r.review.at)}{r.review.note ? `, “${r.review.note}”` : ""}</span> : null}
        {r.version > 1 ? <span>version {r.version}</span> : null}
      </div>
      <ClauseChips items={r.clause_evidence ?? []} onFocusNode={onFocusNode} />
      {r.evidence.length ? <ul className="kg-evidence" aria-label="Evidence">
        {r.evidence.map((e) => {
          const href = codeHref(r.scope.repo, e.path, e.lines);
          return (
            <li key={`${e.symbol_id}-${e.lines?.join("-")}`} className="kg-ev" data-testid="kg-rule-evidence">
              <button type="button" className="kg-ev-chip" onClick={() => onFocusNode(e.symbol_id)} title={e.excerpt ?? e.label}
                      data-testid="kg-evidence-focus">
                <span className="kg-dot" style={{ background: "var(--kg-method)" }} aria-hidden />
                <span className="inf-mono">{e.label}{e.lines ? `:${e.lines[0]}, ${e.lines[1]}` : ""}</span>
              </button>
              {href ? <a className="inf-link" href={href} target="_blank" rel="noreferrer" data-testid="kg-evidence-code">code ↗</a> : null}
              {e.excerpt ? <code className="kg-ev-excerpt" data-user-text>{e.excerpt}</code> : null}
            </li>
          );
        })}
      </ul> : null}
      {proposed && mode !== "edit" ? (
        mode === "reject" ? (
          <form className="kg-reject" data-testid="kg-rule-reject-form" onSubmit={(e) => { e.preventDefault(); void go("reject", () => rejectRule(w, r.node.id, note.trim() || undefined), "Rejected. Agents will not be given it."); }}>
            <label className="kg-field"><span>Why (optional)</span><input className="input" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Why it is rejected" data-testid="kg-reject-note" placeholder="e.g. Only true for tranche loans" /></label>
            <div className="kg-panel-actions">
              <GatedButton permission={RULE_REVIEW} type="submit" className="ag-btn" data-tone="reject" data-testid="kg-reject-yes" disabled={busy !== null || archived}>
                {busy === "reject" ? "Rejecting…" : "Reject"}
              </GatedButton>
              <button type="button" className="inf-link" onClick={() => setMode("view")}>Cancel</button>
            </div>
          </form>
        ) : (
          <div className="kg-panel-actions">
            <GatedButton permission={RULE_REVIEW} className="ag-btn ag-btn-primary" data-testid="kg-rule-approve" disabled={busy !== null || archived}
                         title={archived ? archivedWhy : "Approve: every agent may rely on it from now on"}
                         onClick={() => void go("approve", () => approveRule(w, r.node.id), "Approved. Agents rely on it from now on.")}>
              {busy === "approve" ? "Approving…" : "Approve"}
            </GatedButton>
            <GatedButton permission={RULE_REVIEW} className="ag-btn" data-testid="kg-rule-edit-open" disabled={archived}
                         title={archived ? archivedWhy : "Change it, then approve the new version"} onClick={() => setMode("edit")}>Edit</GatedButton>
            <GatedButton permission={RULE_REVIEW} className="ag-btn" data-tone="reject" data-testid="kg-rule-reject" disabled={archived}
                         title={archived ? archivedWhy : "Reject it, with an optional note"} onClick={() => setMode("reject")}>Reject</GatedButton>
          </div>
        )
      ) : null}
    </li>
  );
}

/** The fact promotion queue (was Approvals › Knowledge facts waiting for a person). */
function FactsQueue({ w }: { w: { projectId: string; actor: string } }) {
  const [items, setItems] = useState<Array<{ id: string; node_key: string; body: string }> | null>(null);
  const [error, setError] = useState<{ status: number; text: string; unavailable?: boolean } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await getGraphStatus(w);
    if (r.ok) { setItems(r.data.promotion_candidates ?? []); setError(null); } else setError({ status: r.status, text: r.error, unavailable: r.unavailable });
  }, [w]);
  useEffect(() => { void load(); }, [load]);
  const decide = async (id: string, action: "promote" | "reject") => {
    setBusy(`${id}:${action}`); setMsg(null);
    const r = action === "promote" ? await promoteFact(w, id) : await rejectFact(w, id);
    setBusy(null);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setItems((cur) => (cur ?? []).filter((x) => x.id !== id));
    setMsg({ ok: true, text: action === "promote" ? "Approved: agents treat it as trusted." : "Rejected." });
  };
  return (
    <section className="kg-facts" data-testid="kg-facts" data-area="knowledge-promotion" aria-label="Facts waiting for a person">
      <p className="kg-quiet">Facts agents inferred while they worked. Approving one makes it trusted input for every agent; rejecting closes it.</p>
      {error ? <KgReadError error={error} onRetry={() => void load()} testId="kg-facts-error" what="The fact queue" /> : null}
      {msg ? <p className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"}>{msg.text}</p> : null}
      {items === null && !error ? <ul className="kg-cards" aria-busy="true"><li className="kg-card kg-card-skel"><span className="kg-skel-row" /><span className="kg-skel-row" /></li></ul> : null}
      {items && !items.length ? (
        <div className="kg-stage kg-stage-short" data-testid="kg-facts-empty">
          <div className="kg-empty" role="status">
            <h3 className="kg-empty-title">Nothing waiting for a person</h3>
            <p className="kg-empty-body">When an agent infers a fact while it works, it waits here until someone approves it as trusted input for every agent, or rejects it.</p>
          </div>
        </div>
      ) : null}
      {items?.length ? (
        <ul className="kg-cards">
          {items.map((f) => (
            <li key={f.id} className="kg-card" data-testid="kg-fact-card">
              <div className="kg-card-head">
                <span className="kg-dot" style={{ background: "var(--kg-fact)" }} aria-hidden />
                <span className="kg-card-key inf-mono" title={f.node_key}>{f.node_key}</span>
              </div>
              <p className="kg-card-statement" data-user-text>{f.body}</p>
              <div className="kg-panel-actions">
                <GatedButton permission={FACT_MANAGE} className="ag-btn ag-btn-primary" data-testid="kg-fact-promote" disabled={busy !== null}
                             onClick={() => void decide(f.id, "promote")}>{busy === `${f.id}:promote` ? "Approving…" : "Approve fact"}</GatedButton>
                <GatedButton permission={FACT_MANAGE} className="ag-btn" data-tone="reject" data-testid="kg-fact-reject" disabled={busy !== null}
                             onClick={() => void decide(f.id, "reject")}>{busy === `${f.id}:reject` ? "Rejecting…" : "Reject fact"}</GatedButton>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
