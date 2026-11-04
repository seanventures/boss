"use client";

/**
 * THE PROJECT PAGE (BOSSAPP-810, screen 3): "What should we add?".
 *
 *   header           the project's name; its repo · branch · code mapped ✓
 *   the card         a textarea, four example chips, the estimate, Start — the project's default
 *                    workflow for adding a feature, on the imported repo's branch (a project with
 *                    no repo builds a new app: its project.new default)
 *   what happens     Plan (You approve) · Build · Review & test · Pull request (You merge) · Preview
 *   in this project  each piece of work: its state and the one thing to do
 *
 * One read (GET /v1/projects/{id}/start); Start goes through the one start door
 * (POST /v1/delivery-workflows/start, app/workflows/delivery-api.ts `post`), which asks for
 * consent and answers a refusal for what the project lacks with the checklist and Start anyway.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  allPresets, estimateLine, mappingWords, startBody, startProjectApi, workHref, type StartView,
} from "../../lib/start-project";
import { post } from "../workflows/delivery-api";
import { ReadinessPanel, unaccepted, type Readiness } from "../workflow-readiness-ui";
import "./start-project.css";

/** A preset's brief, when the page was opened from one (`&start_preset=<key>`). */
function presetBrief(): string {
  if (typeof window === "undefined") return "";
  const key = new URLSearchParams(window.location.search).get("start_preset");
  if (!key) return "";
  const p = allPresets().find((x) => x.key === key);
  return p && p.kind === "app" ? p.brief : "";
}

export function ProjectStartCard({ projectId, orgId, actor }: {
  projectId: string; orgId?: string | null; actor?: string;
}) {
  const [view, setView] = useState<StartView | null>(null);
  const [text, setText] = useState("");
  const [seeded, setSeeded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  const [started, setStarted] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const v = await startProjectApi.view(projectId, signal);
      setView(v);
    } catch { /* the card is a help: no view, no card */ }
  }, [projectId]);

  useEffect(() => {
    const ctl = new AbortController();
    void load(ctl.signal);
    return () => ctl.abort();
  }, [load]);

  // The words to start from: a preset's brief, else the description typed at New project.
  useEffect(() => {
    if (!view || seeded) return;
    setSeeded(true);
    const brief = presetBrief();
    if (brief) setText(brief);
    else if (!view.work.length && view.description) setText(view.description);
  }, [view, seeded]);

  // Mapping and work move on their own: look again while something is under way.
  useEffect(() => {
    if (!view) return;
    const moving = (view.repo && !view.repo.mapping.done && view.repo.mapping.state !== "failed")
      || view.work.some((w) => w.tone === "blue");
    if (!moving) return;
    const t = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 15000);
    return () => window.clearInterval(t);
  }, [view, load]);

  if (!view || view.archived) return null;
  const open = unaccepted(refusal, startAnyway);
  const adding = view.start.mode === "feature";

  const start = async () => {
    if (!text.trim() || busy) return;
    setBusy(true); setError(null);
    const r = await post("start", {
      ...startBody(view, text), ...(actor ? { actor } : {}),
      ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
    });
    setBusy(false);
    if (!r.ok) {
      if ("readiness" in r && r.readiness) { setRefusal(r.readiness); return; }
      setError(typeof r.error === "string" ? r.error : r.error ? JSON.stringify(r.error) : "It did not start.");
      return;
    }
    const data = (r.data ?? {}) as { run?: { id?: string }; id?: string };
    setStarted(data.run?.id ?? data.id ?? "started");
    setText(""); setRefusal(null); setStartAnyway([]);
    void load();
  };

  return (
    <section className="sp-page" data-testid="project-start" aria-labelledby="sp-page-name">
      <header className="sp-page-head">
        <h2 id="sp-page-name">{view.name}</h2>
        {view.repo ? (
          <p data-testid="project-start-repo">
            <span className="sp-mono">{view.repo.full_name}</span> · {view.repo.branch} ·{" "}
            <span data-done={view.repo.mapping.done || undefined} data-testid="project-start-mapped">
              {view.repo.mapping.done ? "code mapped ✓" : mappingWords(view.repo.mapping).toLowerCase()}
            </span>
          </p>
        ) : <p className="sp-quiet" data-testid="project-start-repo">New app · BOSS makes its repo</p>}
      </header>

      <div className="sp-ask" data-testid="project-start-ask">
        <label htmlFor="sp-ask-text" className="sp-ask-label">{adding ? "What should we add?" : "What should we build?"}</label>
        <textarea id="sp-ask-text" className="input sp-ask-text" data-testid="project-start-text" rows={3}
                  value={text} placeholder={adding ? "Add an endpoint that lists orders by customer" : "A shift-swap board for a small team"}
                  onChange={(e) => setText(e.target.value)} />
        {adding ? (
          <div className="sp-chips" data-testid="project-start-chips">
            {view.examples.map((ex) => (
              <button key={ex} type="button" className="sp-chip" onClick={() => setText(ex)}>{ex}</button>
            ))}
          </div>
        ) : null}
        {refusal ? (
          <div data-testid="project-start-readiness">
            <ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} />
          </div>
        ) : null}
        <div className="sp-ask-foot">
          <span className="sp-quiet" data-testid="project-start-estimate"
                title={view.start.workflow_name ? `Runs ${view.start.workflow_name}` : undefined}>
            {estimateLine(view.start)}
          </span>
          <GatedButton permission="workflow_run:start" projectId={projectId} className="ag-btn ag-btn-primary"
                       data-testid="project-start-go" disabled={busy || !text.trim() || (!!refusal && open.length > 0)}
                       title={view.start.workflow_name ? `Start ${view.start.workflow_name}` : "Start"}
                       onClick={() => void start()}>
            {busy ? "Starting…" : refusal ? "Start anyway" : "Start"}
          </GatedButton>
        </div>
        {error ? <p className="bx-np-error" role="alert" data-testid="project-start-error">{error}</p> : null}
        {started ? <p className="sp-ok" role="status" data-testid="project-start-started">Started. It shows below.</p> : null}
      </div>

      <div className="sp-next" data-testid="project-start-next" aria-label="What happens next">
        <h3 className="sp-sub">What happens next</h3>
        <ol>
          {view.next.map((s) => (
            <li key={s.key} data-you={s.you || undefined}><b>{s.label}</b><span>{s.who}</span></li>
          ))}
        </ol>
      </div>

      {view.work.length ? (
        <div className="sp-work" data-testid="project-start-work">
          <h3 className="sp-sub">In this project</h3>
          <ul>
            {view.work.map((w) => (
              <li key={w.run_id} data-tone={w.tone} data-testid={`project-start-work-${w.run_id}`}>
                <span className="sp-dot" aria-hidden />
                <span className="sp-work-title">{w.title}</span>
                <span className="sp-work-state">{w.state}</span>
                <a className="sp-link" href={workHref(w, orgId)}
                   {...(w.action.kind === "pr" ? { target: "_blank", rel: "noreferrer" } : {})}>{w.action.label}</a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
