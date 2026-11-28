"use client";

/**
 * Govern › More › Rules book (BOSSAPP-572): read-only searchable rules book generated from code,
 * grouped by category, showing where each rule lives and how it is checked.
 */
import React, { useEffect, useMemo, useState } from "react";
import { BookOpenText, MagnifyingGlass, ShieldCheck } from "@phosphor-icons/react";

import { readRulesBook, type EnforcementKind, type RuleBookEntry, type RulesBookView } from "../../lib/rules-book";

const ENFORCEMENT_LABELS: Record<EnforcementKind, string> = {
  setting: "Project setting",
  gate: "Code gate",
  policy: "Code policy",
  prompt_only: "Prompt only",
};

const CATEGORY_LABELS: Record<string, string> = {
  autonomy: "Autonomy",
  build: "Build",
  review: "Review",
  deploy: "Deploy",
  security: "Security",
  git: "Git",
  cost: "Cost",
  ux: "UX",
  ops: "Operations",
};

function labelCategory(cat: string): string {
  return CATEGORY_LABELS[cat] || (cat ? cat.charAt(0).toUpperCase() + cat.slice(1) : "Other");
}

export function RulesBookTab({ projectId }: { projectId: string }) {
  const [view, setView] = useState<RulesBookView | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState("");
  const [role, setRole] = useState("");
  const [enforcement, setEnforcement] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    void readRulesBook(projectId).then((res) => {
      if (!alive) return;
      setLoading(false);
      if (res.ok) setView(res.data);
      else setError(res.error);
    });
    return () => { alive = false; };
  }, [projectId]);

  const filtered = useMemo<RuleBookEntry[]>(() => {
    if (!view) return [];
    const query = q.trim().toLowerCase();
    return view.rules.filter((r) => {
      if (category && r.category !== category) return false;
      if (enforcement && r.enforcement.kind !== enforcement) return false;
      if (role && !r.roles.includes("all") && !r.roles.includes(role)) return false;
      if (query) {
        const hay = [r.id, r.title, r.text, r.why, r.category, r.source, r.enforcement.where, r.roles.join(" ")].join(" ").toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    });
  }, [view, q, category, role, enforcement]);

  const grouped = useMemo(() => {
    const groups: { category: string; items: RuleBookEntry[] }[] = [];
    const map = new Map<string, RuleBookEntry[]>();
    for (const r of filtered) {
      const list = map.get(r.category);
      if (list) list.push(r);
      else {
        const next = [r];
        map.set(r.category, next);
        groups.push({ category: r.category, items: next });
      }
    }
    return groups;
  }, [filtered]);

  if (loading && !view) {
    return <div className="gv-skel gv-skel-short" aria-busy="true" data-testid="gv-rb-loading" />;
  }
  if (error && !view) {
    return <div className="gv-muted" role="alert" data-testid="gv-rb-error">{error}</div>;
  }
  if (!view) return null;

  return (
    <section className="gv-card gv-rb" aria-labelledby="gv-rb-h" data-testid="gv-rules-book">
      <header className="gv-rb-head">
        <span className="gv-jr-icon" aria-hidden><BookOpenText size={18} weight="bold" /></span>
        <h3 id="gv-rb-h" className="gv-card-title">Rules book</h3>
        <span className="gv-sub-tag" data-testid="gv-rb-total">{filtered.length} of {view.total}</span>
        <span className="gv-sub-tag" title="Generated from BOSS code; read-only">Read-only</span>
      </header>

      <div className="gv-rb-filters">
        <label className="gv-rb-search">
          <MagnifyingGlass size={14} aria-hidden />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search rules, categories, files"
            aria-label="Search rules book"
            data-testid="gv-rb-search"
          />
        </label>
        <select
          className="gv-rb-select"
          value={role}
          onChange={(e) => setRole(e.target.value)}
          aria-label="Filter by role"
          data-testid="gv-rb-role"
        >
          <option value="">All roles</option>
          {view.roles.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select
          className="gv-rb-select"
          value={enforcement}
          onChange={(e) => setEnforcement(e.target.value)}
          aria-label="Filter by check type"
          data-testid="gv-rb-enforcement"
        >
          <option value="">All check types</option>
          {(["setting", "gate", "policy", "prompt_only"] as EnforcementKind[]).map((k) => (
            <option key={k} value={k}>{ENFORCEMENT_LABELS[k]}</option>
          ))}
        </select>
      </div>

      <div className="gv-rb-cats" role="group" aria-label="Categories" data-testid="gv-rb-categories">
        <button
          type="button"
          className="gv-rb-cat"
          aria-pressed={category === ""}
          onClick={() => setCategory("")}
          data-testid="gv-rb-cat-all"
        >
          All · {view.total}
        </button>
        {view.categories.map((c) => (
          <button
            key={c}
            type="button"
            className="gv-rb-cat"
            aria-pressed={category === c}
            onClick={() => setCategory(category === c ? "" : c)}
            data-testid={`gv-rb-cat-${c}`}
          >
            {labelCategory(c)} · {view.counts_by_category[c] || 0}
          </button>
        ))}
      </div>

      {!grouped.length ? (
        <div className="gv-muted" data-testid="gv-rb-empty">No matching rules.</div>
      ) : (
        <div className="gv-rb-groups">
          {grouped.map((g) => (
            <div key={g.category} className="gv-rb-group" data-testid={`gv-rb-group-${g.category}`}>
              <h4 className="gv-rb-group-h">{labelCategory(g.category)}</h4>
              <ul className="gv-rb-list">
                {g.items.map((r) => (
                  <li key={r.id} className="gv-rb-item" data-testid={`gv-rb-rule-${r.id}`}>
                    <div className="gv-rb-item-head">
                      <b data-user-text>{r.title}</b>
                      <code className="gv-rb-id">{r.id}</code>
                      <span
                        className="gv-rb-badge"
                        data-kind={r.enforcement.kind}
                        title={r.enforcement.where}
                        data-testid={`gv-rb-badge-${r.id}`}
                      >
                        <ShieldCheck size={12} aria-hidden />
                        {ENFORCEMENT_LABELS[r.enforcement.kind]}
                      </span>
                      {r.universal ? <span className="gv-sub-tag">Universal</span> : null}
                    </div>
                    <span className="gv-rb-text" data-user-text>{r.text}</span>
                    <span className="gv-rb-why" data-user-text>Why: {r.why}</span>
                    <div className="gv-rb-meta">
                      <span className="gv-sub-faint" data-user-text>Where: {r.enforcement.where}</span>
                      <span className="gv-sub-faint">Roles: {r.roles.join(", ")}</span>
                      <span className="gv-sub-faint">Source: {r.source}</span>
                      {r.effective_value !== undefined ? (
                        <span className="gv-sub-tag" data-user-text>Current: {String(r.effective_value)}</span>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
