"use client";

/**
 * RIGHT AFTER CREATE (new-project-v2 c-after-create.html): where Create and start lands for an
 * idea — the new project in its first stage, with the PRD as the first thing a person does.
 *
 *   Idea to PR      Add project ✓ · Create and start ✓ · then the first run's stations, live
 *   Now             whose move it is
 *   PRD             the confirmed brief's sections, ticked as the PRD will carry them; the
 *                   approval itself is the one in Approvals / Needs you
 *   What you started stack · workflow · connectors · staging · tell — changed in Settings,
 *                   never by going back
 *   Add a feature   lives here now: Project › New work (Workflows › New build has moved)
 *
 * One read: GET /v1/projects/{id}/add-project/started. A project made another way has no seed
 * and shows nothing here.
 */
import React, { useEffect, useState } from "react";
import { Check, CursorClick, FileText, Hand, Plus, Sliders, Rocket } from "@phosphor-icons/react";

import { enc, idGet } from "../../lib/identity/api";
import type { Brief } from "../../lib/add-project";
import { consoleHref } from "../console-href";
import { Face, Ico, Tag } from "./kit";
import type { PxCtx } from "./project-page";
import "./started.css";

type Node = { key: string; label: string; type: string; state: "done" | "now" | "you" | "todo" | "failed"; agent?: { name: string; role_key?: string } | null };
type Started = {
  seed: null | { kind: string; words: string; brief: Partial<Brief>; picks: Record<string, unknown>; created_at: string | null };
  run?: { id: string; status: string } | null; nodes?: Node[]; now?: { label: string; you: boolean; text: string } | null;
  workflow?: { key: string; name: string; steps: number; approvals: number } | null;
};

const LIVE = new Set(["queued", "validating", "running", "waiting", "paused", "blocked"]);

export function StartedCards({ ctx }: { ctx: PxCtx }) {
  const [v, setV] = useState<Started | null>(null);
  useEffect(() => {
    let alive = true;
    const read = () => idGet<Started>(`/v1/projects/${enc(ctx.projectId)}/add-project/started`).then((s) => { if (alive) setV(s); }).catch(() => undefined);
    void read();
    const t = window.setInterval(() => { if (document.visibilityState === "visible") void read(); }, 15000);
    return () => { alive = false; window.clearInterval(t); };
  }, [ctx.projectId]);
  if (!v?.seed || v.seed.kind !== "idea") return null;
  // Shown while the first run is under way (or waits on its PRD): the project's first stage.
  if (v.run && !LIVE.has(v.run.status)) return null;
  const b = v.seed.brief ?? {};
  const picks = v.seed.picks ?? {};
  const connectors = (picks.connectors as string[] | undefined) ?? [];
  const staging = picks.staging as { host_id: string; port: number | null } | null | undefined;
  const notify = (picks.notify as { channel: string | null } | undefined)?.channel;
  const nodes = v.nodes ?? [];
  const prdNow = nodes.some((n) => n.key === "approve" && n.state === "you");
  const decisions = consoleHref({ area: "decisions" });
  return (
    <div className="px-stack" data-testid="px-started">
      <section className="px-card" aria-labelledby="px-st-path">
        <div className="px-card-h"><Ico icon={CursorClick} c="teal" /><h2 id="px-st-path">Idea to PR</h2>
          <Tag tone="blue">2 clicks + your gates</Tag></div>
        <ol className="px-st-path" aria-label="From the idea to the pull request" data-testid="px-started-path">
          <li data-state="done"><span className="px-st-n">1</span>Add project</li>
          <li data-state="done"><span className="px-st-n">2</span>Create and start</li>
          {nodes.map((n) => (
            <li key={n.key} data-state={n.state} data-you={n.type === "you" || undefined} title={n.agent?.name ? `${n.label} · ${n.agent.name}` : n.label}>
              {n.state === "done" ? <Check size={12} weight="bold" aria-hidden /> : n.type === "you" ? <Hand size={12} weight="fill" aria-hidden />
                : n.agent ? <Face agent={n.agent} size={16} state={n.state === "now" ? "working" : "idle"} /> : null}
              {n.type === "you" ? (n.key === "approve" ? "Approve PRD" : n.label) : n.label}
            </li>
          ))}
        </ol>
        {v.now ? (
          <p className="px-st-now" data-you={v.now.you || undefined} data-testid="px-started-now">
            <b>Now</b> {v.now.text}
            {v.run ? <a className="px-more" href={consoleHref({ area: "delivery_workflows", wfr: v.run.id })}>Watch it</a> : null}
          </p>
        ) : null}
      </section>

      <div className="px-grid px-g3">
        <section className="px-card" data-you={prdNow || undefined} aria-labelledby="px-st-prd" data-testid="px-started-prd">
          <div className="px-card-h"><Ico icon={FileText} c="amber" /><h2 id="px-st-prd">PRD</h2>
            <Tag tone={prdNow ? "amber" : "blue"}>{prdNow ? "waiting for you" : "drafting"}</Tag></div>
          <ul className="px-st-list">
            <li data-ok={!!(b.problem || b.who_uses) || undefined}><b>Problem &amp; users</b><span>{b.who_uses || b.problem || "from your words"}</span></li>
            <li data-ok={!!b.must_haves?.length || undefined}><b>Must-haves</b><span>{(b.must_haves ?? []).slice(0, 3).join(" · ") || "from your words"}</span></li>
            <li data-ok={!!connectors.length || undefined}><b>Connectors</b><span>{connectors.join(" · ") || "none picked"}</span></li>
            <li data-ok={!!b.done_when || undefined}><b>Done when</b><span>{b.done_when || "the panel drafts it"}</span></li>
            {(b.open_questions ?? []).filter((q) => !q.answer).length ? <li><b>Open</b><span>{(b.open_questions ?? []).filter((q) => !q.answer).map((q) => q.question).join(" · ")}</span></li> : null}
          </ul>
          <div className="px-row" style={{ marginTop: 10 }}>
            <a className="px-btn" data-kind={prdNow ? "amber" : undefined} href={decisions}>{prdNow ? "Approve PRD" : "Approvals"}</a>
            <a className="px-btn" data-kind="ghost" href={decisions}>Comment</a>
          </div>
        </section>

        <section className="px-card" aria-labelledby="px-st-what" data-testid="px-started-what">
          <div className="px-card-h"><Ico icon={Sliders} c="violet" /><h2 id="px-st-what">What you started</h2></div>
          <dl className="px-st-dl">
            <dt>Stack</dt><dd>{String(picks.stack || "BOSS picks at the plan")}</dd>
            <dt>Workflow</dt><dd>{v.workflow ? <>{v.workflow.name} · {v.workflow.steps} · {v.workflow.approvals} <Hand size={11} weight="fill" aria-label="you approve" /></> : String(picks.workflow_key || "the default")}</dd>
            <dt>Connectors</dt><dd>{connectors.join(" · ") || "none"}</dd>
            <dt>Staging</dt><dd>{staging ? `${staging.host_id}${staging.port ? ` · :${staging.port}` : ""}` : "preview only"}</dd>
            <dt>Tell</dt><dd>{notify ? notify.replace(/^email:/, "") : "you · email"}</dd>
          </dl>
          <button type="button" className="px-btn" data-kind="ghost" onClick={() => ctx.tab("settings")}>Change in Settings →</button>
        </section>

        <section className="px-card" aria-labelledby="px-st-add" data-testid="px-started-add">
          <div className="px-card-h"><Ico icon={Plus} c="blue" /><h2 id="px-st-add">Add a feature lives here</h2></div>
          <p className="px-row px-wrap" style={{ margin: "0 0 10px" }}>
            <s className="px-faint">Workflows › New build › Add a feature</s> <span aria-hidden>→</span>
            <button type="button" className="px-btn" data-kind="primary" data-testid="px-started-new-work" onClick={() => ctx.tab("work", { compose: "1" })}><Rocket size={12} aria-hidden /> New work</button>
          </p>
          <p className="px-faint" style={{ margin: 0 }}>Builds onto the app’s branch · Feature · Bug · Ticket, with Helm’s plan before Start.</p>
        </section>
      </div>
    </div>
  );
}
