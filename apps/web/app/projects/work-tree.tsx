"use client";

/**
 * THE WORK TREE (BOSSAPP-830/831): the project's one work-item graph as a tree — epics, their
 * stories and tasks, and the items each workflow run, build, PRD panel and planning ticket
 * belongs to. Each row: its key, title, a status chip, who works it, its labels and a
 * "Blocked by" badge naming what it waits for. A row opens in place: status and Mark done (the
 * execution policy may answer "In review"), Blocked by, the policy editor (its review →
 * approval stages, as JSON) and the stage timeline. The Work tab's third view, beside List and
 * Board.
 */
import React, { useCallback, useEffect, useState } from "react";
import { CaretDown, CaretRight, LinkSimple, Plus, TreeStructure } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  POLICY_EXAMPLE, STATUS_WORDS, completeItem, createItem, flatten, partyName, readItem, readPolicy,
  readTree, relateItem, updateItem, type WorkNode, type WorkStatus, type WorkTree,
} from "../../lib/work-items";
import { WatchSection } from "./work-item-extras";
import "./work-tree.css";

const SETTABLE: WorkStatus[] = ["backlog", "todo", "active", "blocked", "cancelled"];

export function StatusChip({ status }: { status: WorkStatus }) {
  return <span className="wt-chip" data-s={status} data-testid="wt-status">{STATUS_WORDS[status]}</span>;
}

export function WorkTreeView({ projectId, demo = false }: { projectId: string; demo?: boolean }) {
  const [tree, setTree] = useState<WorkTree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [title, setTitle] = useState("");
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await readTree(projectId);
    if (r.ok && r.data) { setTree(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "This install has no work tree yet." : r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const add = async () => {
    if (!title.trim()) return;
    const r = await createItem(projectId, { title: title.trim(), parent_id: open });
    setSaid(r.ok ? null : r.error);
    if (r.ok) { setTitle(""); void load(); }
  };

  if (error) return <div className="px-card wt-empty" data-testid="wt-error">{error}</div>;
  if (!tree) return <div className="px-card wt-empty" data-testid="wt-loading">Loading the work tree…</div>;
  const rows = flatten(tree.items).filter(({ node }) => !hiddenBy(node, tree, folded));
  const all = flatten(tree.items).map((r) => r.node);
  return (
    <div className="wt" data-testid="wt">
      <div className="wt-bar">
        <span className="wt-counts" aria-label="By status">
          {(Object.keys(STATUS_WORDS) as WorkStatus[]).filter((s) => tree.by_status[s]).map((s) => (
            <span key={s}><StatusChip status={s} /> {tree.by_status[s]}</span>
          ))}
        </span>
        <span className="px-spacer" />
        <input className="input wt-new" value={title} onChange={(e) => setTitle(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
               placeholder={open ? "New item under the open one" : "New work item"} aria-label="New work item title"
               data-testid="wt-new-title" disabled={demo} />
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn"
                     disabled={demo || !title.trim()} onClick={() => void add()} data-testid="wt-add">
          <Plus size={12} aria-hidden /> Add
        </GatedButton>
      </div>
      {said ? <p className="wt-said" role="alert">{said}</p> : null}
      {!rows.length ? (
        <div className="px-card wt-empty" data-testid="wt-empty">
          <TreeStructure size={18} aria-hidden /> No work items yet. Add one above; runs and builds join it as they start.
        </div>
      ) : (
        <ul className="wt-rows" role="tree" aria-label="Work items">
          {rows.map(({ node, depth }) => (
            <li key={node.id} role="treeitem" aria-expanded={node.children.length ? !folded.has(node.id) : undefined}
                aria-selected={open === node.id} data-testid={`wt-row-${node.id}`}>
              <div className="wt-row" style={{ paddingLeft: 8 + depth * 18 }}>
                {node.children.length ? (
                  <button type="button" className="wt-fold" aria-label={folded.has(node.id) ? "Expand" : "Collapse"}
                          onClick={() => setFolded((f) => toggle(f, node.id))}>
                    {folded.has(node.id) ? <CaretRight size={11} /> : <CaretDown size={11} />}
                  </button>
                ) : <span className="wt-fold" aria-hidden />}
                <button type="button" className="wt-open" onClick={() => setOpen(open === node.id ? null : node.id)}>
                  {node.human_key ? <span className="wt-key">{node.human_key}</span> : null}
                  <span className="wt-title">{node.title}</span>
                </button>
                <StatusChip status={node.status} />
                {node.blocked_by.length ? (
                  <span className="wt-blocked" data-testid="wt-blocked"
                        title={`Waits for ${node.blocked_by.map((b) => b.human_key || b.title).join(", ")}`}>
                    Blocked by {node.blocked_by.map((b) => b.human_key || b.title).join(", ")}
                  </span>
                ) : null}
                {node.labels.map((l) => <span key={l.name} className="wt-label">{l.name}</span>)}
                {node.assignee ? <span className="wt-who" title={node.assignee}>{partyName(node.assignee)}</span> : null}
              </div>
              {open === node.id ? (
                <ItemPanel projectId={projectId} id={node.id} all={all} demo={demo} onChanged={() => void load()} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function toggle(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id); else next.add(id);
  return next;
}

/** A row is hidden when any ancestor is folded. */
function hiddenBy(node: WorkNode, tree: WorkTree, folded: Set<string>): boolean {
  if (!folded.size) return false;
  const parents = new Map(flatten(tree.items).map(({ node: n }) => [n.id, n.parent_id]));
  let p = node.parent_id;
  while (p) {
    if (folded.has(p)) return true;
    p = parents.get(p) ?? null;
  }
  return false;
}

/** One item, opened in place: status, done, blocked-by, the policy editor and its stage timeline. */
export function ItemPanel({ projectId, id, all, demo, onChanged }: {
  projectId: string; id: string; all: WorkNode[]; demo: boolean; onChanged: () => void;
}) {
  const [item, setItem] = useState<WorkNode | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [policyText, setPolicyText] = useState("");
  const [blocker, setBlocker] = useState("");
  const load = useCallback(async () => {
    const r = await readItem(projectId, id);
    if (r.ok && r.data) {
      setItem(r.data);
      setPolicyText(r.data.execution_policy ? JSON.stringify(r.data.execution_policy, null, 2) : "");
    }
  }, [projectId, id]);
  useEffect(() => { void load(); }, [load]);
  if (!item) return <div className="wt-panel" data-testid="wt-panel-loading">Loading…</div>;
  const policy = readPolicy(policyText);
  const after = async (r: { ok: boolean; error: string | null; body?: unknown }, ok?: string) => {
    setSaid(r.ok ? ok ?? null : r.error);
    if (r.ok) { await load(); onChanged(); }
  };
  const review = (item.execution_state?.review ?? null) as { feedback?: string; round?: number } | null;
  return (
    <div className="wt-panel" data-testid="wt-panel">
      {item.description ? <p className="wt-desc">{item.description}</p> : null}
      <div className="wt-line">
        <label className="wt-field">Status
          <select className="input" value={SETTABLE.includes(item.status) ? item.status : ""} disabled={demo || item.status === "in_review"}
                  onChange={(e) => void updateItem(projectId, id, { status: e.target.value }, "Status").then((r) => after(r))}
                  data-testid="wt-status-select">
            {SETTABLE.includes(item.status) ? null : <option value="">{STATUS_WORDS[item.status]}</option>}
            {SETTABLE.map((s) => <option key={s} value={s}>{STATUS_WORDS[s]}</option>)}
          </select>
        </label>
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn"
                     disabled={demo || item.status === "done" || item.status === "in_review"}
                     onClick={() => void completeItem(projectId, id).then((r) => after(r, (r.body as { status?: string } | null)?.status === "in_review"
                       ? "In review — its first stage is in Needs you." : "Done."))}
                     data-testid="wt-done">
          Mark done
        </GatedButton>
        {item.responsible_person ? <span className="wt-faint">Answers for it: {partyName(item.responsible_person)}</span> : null}
      </div>
      {review?.feedback ? (
        <p className="wt-feedback" data-testid="wt-feedback">Sent back (round {review.round ?? 1}): {review.feedback}</p>
      ) : null}
      <div className="wt-line">
        <LinkSimple size={12} aria-hidden />
        <span className="wt-faint">Blocked by</span>
        {item.blocked_by.map((b) => <span key={b.id} className="wt-blocked">{b.human_key || b.title}</span>)}
        <select className="input" value={blocker} onChange={(e) => setBlocker(e.target.value)} aria-label="Add a blocker"
                data-testid="wt-blocker-select" disabled={demo}>
          <option value="">Add a blocker…</option>
          {all.filter((n) => n.id !== id && !item.blocked_by.some((b) => b.id === n.id)).map((n) => (
            <option key={n.id} value={n.id}>{n.human_key ? `${n.human_key} · ` : ""}{n.title}</option>
          ))}
        </select>
        <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn" disabled={demo || !blocker}
                     onClick={() => void relateItem(projectId, id, "blocked_by", blocker).then((r) => { setBlocker(""); return after(r); })}
                     data-testid="wt-blocker-add">
          Link
        </GatedButton>
      </div>
      <details className="wt-policy" open={Boolean(item.execution_policy)}>
        <summary>Review &amp; approval stages {item.execution_policy ? `· ${item.execution_policy.stages.length}` : "· none"}</summary>
        <p className="wt-faint">Done waits for these, in order. A send-back returns it with the comment; after max_rounds it goes to the responsible person.</p>
        <textarea className="textarea wt-json" rows={6} value={policyText} placeholder={POLICY_EXAMPLE}
                  onChange={(e) => setPolicyText(e.target.value)} aria-label="Execution policy (JSON)"
                  data-testid="wt-policy" disabled={demo} />
        <div className="wt-line">
          {policy.error ? <span className="wt-said" data-testid="wt-policy-error">{policy.error}</span> : null}
          <GatedButton permission="work_item:manage" projectId={projectId} className="px-btn"
                       disabled={demo || Boolean(policy.error)}
                       onClick={() => void updateItem(projectId, id, { execution_policy: policy.policy }, "Save stages").then((r) => after(r, "Stages saved."))}
                       data-testid="wt-policy-save">
            Save stages
          </GatedButton>
        </div>
      </details>
      {item.stages?.length ? (
        <ol className="wt-timeline" aria-label="Stage timeline" data-testid="wt-timeline">
          {item.stages.map((s, i) => (
            <li key={`${s.event}-${i}`} data-e={s.event}>
              <span className="wt-ev">{EVENT_WORDS[s.event] ?? s.event}</span>
              <span>{s.stage_kind} {s.stage_index + 1}{s.round > 1 ? ` · round ${s.round}` : ""}</span>
              {s.actor && s.actor !== "boss" ? <span className="wt-faint">{partyName(s.actor) || s.actor}</span> : null}
              {s.comment ? <span className="wt-comment">“{s.comment}”</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
      {item.runs?.length ? (
        <p className="wt-faint" data-testid="wt-runs">
          Runs: {item.runs.map((r) => `${RUN_WORDS[r.kind] ?? r.kind} ${r.status}`).join(" · ")}
        </p>
      ) : null}
      <WatchSection projectId={projectId} id={id} demo={demo} />
      {said ? <p className="wt-said" role="status" data-testid="wt-said">{said}</p> : null}
    </div>
  );
}

const EVENT_WORDS: Record<string, string> = {
  raised: "Asked", approved: "Approved", rejected: "Sent back", escalated: "Escalated",
  completed: "Done", skipped: "Skipped",
};
const RUN_WORDS: Record<string, string> = {
  workflow_run: "Workflow", code_task: "Build", meeting: "Panel", roadmap_item: "Ticket",
};
