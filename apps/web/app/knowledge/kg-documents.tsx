"use client";

/**
 * KNOWLEDGE GRAPH › DOCUMENTS (KG P7a): the regulation documents and clauses in a project's
 * Knowledge Graph. The pack row (with Change over the shipped packs), an "Add regulation" form
 * (title, citation, URL, and one section + exact text per clause, paste-only — BOSS never fetches
 * a URL or segments a blob), and the list of documents and their clauses. Each clause links to its
 * node in Graph, and offers "Check compliance" — starting banking_regulatory_compliance with the
 * clause's implementing rule as subject when there is exactly one, asking when there are several.
 *
 * One door for every read and write: lib/kg-view.ts. Writes are pre-greyed on knowledge:manage
 * (components/gated.tsx); the workflow start reuses app/workflows/delivery-api.ts `post("start")`.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  DOCUMENT_MANAGE, addDocument, getDocuments, getLinkedRules, getPacks, getRules, getSearch, kgNodeHref, kgWhen, linkRule, setPack,
  type KgClause, type KgDocument, type KgPack,
} from "../../lib/kg-view";
import { SCAN_START } from "../../lib/kg-view";
import { post } from "../workflows/delivery-api";
import { AmlSample } from "./kg-aml-sample";

type Who = { projectId: string; actor: string };

/** One clause's "Check compliance": reads its implementing rules, then starts the pipeline with
 *  the single rule as subject, asks when there are several, or starts with none. */
const NO_WORKFLOW_RE = /no delivery workflow/i;
const NEEDS_SUBJECT_RE = /needs .* input.*subject_id/i;

function CheckCompliance({ w, documentId, clauseId, archived }: {
  w: Who; documentId: string; clauseId: string; archived?: boolean;
}) {
  const [rules, setRules] = useState<{ node_id: string; title: string }[] | null>(null);
  const [picking, setPicking] = useState(false);
  const [chosen, setChosen] = useState<string | "">("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [needsInstall, setNeedsInstall] = useState(false);
  const [needsSubject, setNeedsSubject] = useState(false);
  const [pendingSubjectId, setPendingSubjectId] = useState<string | null>(null);
  const [candidateRules, setCandidateRules] = useState<{ node_id: string; title: string }[]>([]);
  const [linkChosen, setLinkChosen] = useState<string | "">("");
  const [ruleQuery, setRuleQuery] = useState("");
  const [searching, setSearching] = useState(false);

  const start = useCallback(async (subjectId?: string | null) => {
    setBusy(true); setMsg(null);
    setPendingSubjectId(subjectId ?? null);
    const input: Record<string, unknown> = { document_id: documentId };
    if (subjectId) input.subject_id = subjectId;
    const r = await post("start", {
      project_id: w.projectId, workflow_key: "banking_regulatory_compliance", actor: w.actor, input,
      // The project's own copy (e.g. with its bank's agent added) when it has one.
      prefer_project_copy: true,
    });
    setBusy(false);
    if (r.ok) {
      setPicking(false); setNeedsInstall(false); setNeedsSubject(false);
      setMsg({ ok: true, text: "Compliance check started. Its run opens in Workflows." });
      return;
    }
    const err = r.error ? String(r.error) : "";
    // Both of these are offered a one-press recovery that reuses an existing endpoint, rather
    // than showing the raw error: a project predating the catalogue entry (install it), and a
    // clause with no rule linked yet (pick one).
    if (NO_WORKFLOW_RE.test(err)) { setNeedsInstall(true); return; }
    if (NEEDS_SUBJECT_RE.test(err)) {
      setNeedsSubject(true);
      // The clause has nothing linked yet — offer the project's existing business rules to
      // pick from, not the clause's (empty) linked-rules list.
      const found = await getRules(w, { status: "approved" });
      setCandidateRules(found.ok
        ? found.data.items.map((rule) => ({ node_id: rule.node.id, title: rule.title })) : []);
      return;
    }
    setMsg({ ok: false, text: err || "The compliance check did not start." });
  }, [documentId, w]);

  // The initial "approved" rules list can miss the clause's implementing rule (not yet
  // approved, or named differently) — a free-text search over the project's rules lets the
  // operator find it instead of being stuck with that fixed list.
  const searchRules = useCallback(async () => {
    const q = ruleQuery.trim();
    if (!q) return;
    setSearching(true);
    const found = await getSearch(w, q, { layers: "rules" });
    setSearching(false);
    if (!found.ok) return;
    const hits = found.data.hits
      .filter((hit) => hit.node.kind === "rule" || hit.node.layer === "rules")
      .map((hit) => ({ node_id: hit.node.id, title: hit.node.label }));
    setCandidateRules((prev) => {
      const byId = new Map(prev.map((r) => [r.node_id, r]));
      for (const hit of hits) byId.set(hit.node_id, hit);
      return [...byId.values()];
    });
  }, [ruleQuery, w]);

  // Picking a rule must PERSIST the clause→rule link (criterion 6: a later check must find it
  // linked), not just pass it as this one run's subject_id — so the write happens first, through
  // the same `implemented_by` edge `regulatory_trace` proposes, and only then does start retry.
  const linkAndStart = useCallback(async () => {
    if (!linkChosen) return;
    setBusy(true); setMsg(null);
    const linked = await linkRule(w, clauseId, linkChosen);
    setBusy(false);
    if (!linked.ok) { setMsg({ ok: false, text: linked.error }); return; }
    setNeedsSubject(false);
    void start(linkChosen);
  }, [w, clauseId, linkChosen, start]);

  const installAndRetry = useCallback(async () => {
    setBusy(true);
    const r = await post("catalogue/install", { project_id: w.projectId });
    setBusy(false);
    if (r.ok) { setNeedsInstall(false); void start(pendingSubjectId); return; }
    setMsg({ ok: false, text: r.error ? String(r.error) : "Could not install the workflow." });
  }, [w.projectId, start, pendingSubjectId]);

  const onPress = async () => {
    if (archived) return;
    setNeedsInstall(false); setNeedsSubject(false); setCandidateRules([]); setLinkChosen("");
    const r = await getLinkedRules(w, clauseId);
    const found = r.ok ? r.data.rules : [];
    setRules(found);
    if (found.length <= 1) { void start(found[0]?.node_id ?? null); return; }
    setChosen(found[0].node_id);
    setPicking(true);
  };

  return (
    <span className="kg-doc-check">
      <GatedButton permission={SCAN_START} className="ag-btn" data-testid="kg-doc-check" disabled={archived || busy}
                   title="Start the banking regulatory compliance pipeline for this clause"
                   onClick={() => void onPress()}>Check compliance</GatedButton>
      {picking && rules && rules.length > 1 ? (
        <span className="kg-doc-pick" data-testid="kg-doc-pick">
          <select value={chosen} onChange={(e) => setChosen(e.target.value)} aria-label="Business rule to check">
            {rules.map((rule) => <option key={rule.node_id} value={rule.node_id}>{rule.title}</option>)}
          </select>
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy || !chosen}
                  onClick={() => void start(chosen)}>Start</button>
        </span>
      ) : null}
      {needsInstall ? (
        <span className="kg-doc-install" data-testid="kg-doc-install-workflow-wrap">
          <button type="button" className="ag-btn ag-btn-primary" data-testid="kg-doc-install-workflow"
                  disabled={busy} title="Install banking_regulatory_compliance from the catalogue"
                  onClick={() => void installAndRetry()}>Install from catalogue</button>
        </span>
      ) : null}
      {needsSubject ? (
        <span className="kg-doc-link" data-testid="kg-doc-link-subject">
          <input className="input" value={ruleQuery} placeholder="Search rules by name…"
                 data-testid="kg-doc-link-search" onChange={(e) => setRuleQuery(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void searchRules(); } }} />
          <button type="button" className="ag-btn" data-testid="kg-doc-link-search-btn"
                  disabled={searching || !ruleQuery.trim()} onClick={() => void searchRules()}>Search</button>
          <select value={linkChosen} onChange={(e) => setLinkChosen(e.target.value)} aria-label="Business rule to link">
            <option value="">Choose a rule…</option>
            {candidateRules.map((rule) => <option key={rule.node_id} value={rule.node_id}>{rule.title}</option>)}
          </select>
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy || !linkChosen}
                  onClick={() => void linkAndStart()}>Link rule &amp; start</button>
        </span>
      ) : null}
      {msg ? <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-doc-check-msg">{msg.text}</span> : null}
    </span>
  );
}

function ClauseRow({ w, document, clause, archived }: {
  w: Who; document: KgDocument; clause: KgClause; archived?: boolean;
}) {
  return (
    <li className="kg-doc-clause" data-testid="kg-doc-clause">
      <a className="inf-link kg-doc-clause-section" href={kgNodeHref(clause.id)} data-testid="kg-doc-clause-link">{clause.section}</a>
      <span className="kg-doc-clause-text" data-user-text>{clause.text}</span>
      {clause.citation ? <span className="kg-quiet">{clause.citation}</span> : null}
      <CheckCompliance w={w} documentId={document.id} clauseId={clause.id} archived={archived} />
    </li>
  );
}

/** The pack row and its Change picker over the shipped packs. */
function PackRow({ w, current, available, onChanged, archived }: {
  w: Who; current: KgPack | null; available: KgPack[]; onChanged: () => void; archived?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<string>(current?.pack_id ?? available[0]?.pack_id ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    if (!pick) return;
    setBusy(true); setErr(null);
    const r = await setPack(w, pick);
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setOpen(false);
    onChanged();
  };

  return (
    <div className="kg-doc-row" data-testid="kg-doc-pack">
      <span className="kg-doc-pack-label">Ontology pack</span>
      <span className="kg-doc-pack-value" data-testid="kg-doc-pack-value">
        {current ? `${current.pack_id} (v${current.version})` : "No pack assigned"}
      </span>
      <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-doc-pack-change" disabled={archived}
                   title="Choose the ontology pack this project's Knowledge Graph accepts" onClick={() => setOpen((v) => !v)}>
        Change
      </GatedButton>
      {open ? (
        <span className="kg-doc-pick" data-testid="kg-doc-pack-pick">
          <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Ontology pack">
            {available.map((p) => <option key={p.pack_id} value={p.pack_id}>{p.pack_id} (v{p.version})</option>)}
          </select>
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy || !pick} onClick={() => void save()}>Save</button>
          {err ? <span className="kg-inline-err" role="alert" data-testid="kg-doc-pack-err">{err}</span> : null}
        </span>
      ) : null}
    </div>
  );
}

type Draft = { title: string; citation: string; url: string; clauses: { section: string; text: string }[] };
const EMPTY_DRAFT: Draft = { title: "", citation: "", url: "", clauses: [{ section: "", text: "" }] };

/** The "Add regulation" form: title/citation/URL and one-or-more {section, text} clause rows. */
function AddRegulation({ w, disabled, disabledWhy, onAdded }: {
  w: Who; disabled: boolean; disabledWhy: string; onAdded: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const setClause = (i: number, field: "section" | "text", value: string) =>
    setDraft((d) => ({ ...d, clauses: d.clauses.map((c, j) => (j === i ? { ...c, [field]: value } : c)) }));
  const addClause = () => setDraft((d) => ({ ...d, clauses: [...d.clauses, { section: "", text: "" }] }));

  const valid = draft.title.trim() !== "" && draft.clauses.every((c) => c.section.trim() !== "" && c.text.trim() !== "");

  const submit = async () => {
    if (!valid || disabled) return;
    setBusy(true); setMsg(null);
    const r = await addDocument(w, {
      title: draft.title.trim(), source_url: draft.url.trim(), citation: draft.citation.trim(),
      clauses: draft.clauses.map((c) => ({ section: c.section.trim(), text: c.text.trim() })),
    });
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setDraft(EMPTY_DRAFT);
    setMsg({ ok: true, text: "Regulation added." });
    onAdded();
  };

  return (
    <form className="kg-doc-form" data-testid="kg-doc-form" noValidate onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h3>Add regulation</h3>
      {disabled ? <p className="kg-quiet" data-testid="kg-doc-form-disabled">{disabledWhy}</p> : null}
      <label className="kg-field"><span>Title</span>
        <input className="input" value={draft.title} disabled={disabled} data-testid="kg-doc-title"
               onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))} /></label>
      <label className="kg-field"><span>Citation</span>
        <input className="input" value={draft.citation} disabled={disabled} data-testid="kg-doc-citation"
               onChange={(e) => setDraft((d) => ({ ...d, citation: e.target.value }))} /></label>
      <label className="kg-field"><span>Source URL</span>
        <input className="input" value={draft.url} disabled={disabled} data-testid="kg-doc-url"
               onChange={(e) => setDraft((d) => ({ ...d, url: e.target.value }))} /></label>
      <div className="kg-doc-clauses">
        {draft.clauses.map((c, i) => (
          <div className="kg-doc-clause-fields" key={i}>
            <label className="kg-field"><span>Section</span>
              <input className="input" value={c.section} disabled={disabled} data-testid="kg-doc-clause-section"
                     onChange={(e) => setClause(i, "section", e.target.value)} /></label>
            <label className="kg-field"><span>Exact text</span>
              <textarea className="input" value={c.text} disabled={disabled} data-testid="kg-doc-clause-textarea"
                        onChange={(e) => setClause(i, "text", e.target.value)} /></label>
          </div>
        ))}
        <button type="button" className="inf-link" disabled={disabled} data-testid="kg-doc-add-clause" onClick={addClause}>+ clause</button>
      </div>
      <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn ag-btn-primary" data-testid="kg-doc-submit"
                   disabled={disabled || busy || !valid} title="Add this regulation and its clauses" onClick={() => void submit()}>
        {busy ? "Adding…" : "Add regulation"}
      </GatedButton>
      {msg ? <p className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-doc-submit-msg">{msg.text}</p> : null}
    </form>
  );
}

export function DocumentsTab({ projectId, actor, archived = false }: {
  projectId: string; actor: string; archived?: boolean;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [docs, setDocs] = useState<KgDocument[] | null>(null);
  const [packs, setPacks] = useState<{ current: KgPack | null; available: KgPack[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [d, p] = await Promise.all([getDocuments(w), getPacks(w)]);
    if (d.ok) { setDocs(d.data.documents); setError(null); } else setError(d.error);
    if (p.ok) setPacks({ current: p.data.current, available: p.data.available });
  }, [w]);
  useEffect(() => { void load(); }, [load]);

  const hasPack = !!packs?.current;
  const addDisabled = archived || !hasPack;
  const addWhy = archived
    ? "This project is archived: its documents are read-only."
    : "Assign the banking-regulatory pack first, then add a regulation.";

  return (
    <section className="kg-documents" data-testid="kg-documents" aria-label="Regulation documents">
      {error ? (
        <div className="kg-banner" data-tone="red" role="alert" data-testid="kg-documents-error">
          <span>{error}</span><button type="button" className="inf-link" onClick={() => void load()}>Retry</button>
        </div>
      ) : null}
      {packs ? <PackRow w={w} current={packs.current} available={packs.available} onChanged={() => void load()} archived={archived} /> : null}
      <AddRegulation w={w} disabled={addDisabled} disabledWhy={addWhy} onAdded={() => void load()} />
      <AmlSample projectId={projectId} actor={actor} archived={archived} onLoaded={() => void load()} />
      {docs && docs.length === 0 ? <p className="kg-quiet" data-testid="kg-documents-empty">No regulation documents yet.</p> : null}
      {docs && docs.length > 0 ? (
        <ul className="kg-doc-list" data-testid="kg-doc-list">
          {docs.map((doc) => (
            <li className="kg-doc" key={doc.id} data-testid="kg-doc">
              <div className="kg-doc-head">
                <span className="kg-doc-title" data-user-text>{doc.title}</span>
                {doc.citation ? <span className="kg-quiet">{doc.citation}</span> : null}
                {doc.added_at ? <span className="kg-quiet">{kgWhen(doc.added_at)}</span> : null}
              </div>
              <ul className="kg-doc-clause-list">
                {doc.clauses.map((clause) => <ClauseRow key={clause.id} w={w} document={doc} clause={clause} archived={archived} />)}
              </ul>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
