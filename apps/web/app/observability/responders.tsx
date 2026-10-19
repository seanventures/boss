"use client";

/**
 * WHO DID WHAT — the incident card's account of its response (incident_responders.py).
 *
 *   WHO DID WHAT
 *   (face) Beacon · Site Reliability Engineer        Triage                          19:05
 *          Decide what the alert is and what BOSS does with it
 *          Kept it in this incident  “POST /orders fails on 3-line orders since release 1.2 …”
 *   (face) Beacon · Site Reliability Engineer        Investigate the incident          5 s
 *          Root cause, 90% sure  “Release 1a2b3c4 went out …”
 *          Runbook ▸ The app is serving badly · Evidence: metrics · logs · release · 8 skills ↗
 *   (face) Helm · Engineering Captain                 Build, review and test       PR ↗
 *          Design · Atlas — Plans the change     Write tests · Vera — red on the faulty release
 *          Code review · Rook — survived: “…”    Security check · Sentry — clean: “…”
 *          Regression test test_shipping.py  ● red on the faulty release → ● green with the fix
 *   (you)  Seed Owner · person                        Approve the release   Approved 19:13
 *   (BOSS) BOSS's deploy, for Seed Owner              Deploy               Deployed 1a2b3c4
 *
 * Every agent is named by its callsign beside its role (run-face nameOf) and links to its page in
 * Agent Studio, where its skills are. A step that has not run yet shows its job and nothing it
 * did. One component, used by the card; pure render from the parsed view.
 */
import React, { useState } from "react";

import type { CrewMember, RedGreen, Responder, ResponderWho } from "../../lib/incident-response";
import { useLocalTime } from "../../lib/local-time";
import { RunFace, nameOf } from "../agents/run-face";
import { consoleHref } from "../agents/org-shared";
import { Markdown } from "../markdown";
import { AnswerAgent } from "./answer-agent";

/** The agent's page in Agent Studio, on its skills (its Loadout). */
export function agentPageHref(roleKey: string): string {
  return consoleHref({ area: "workforce", role: roleKey, tab: "skills", view: null, focus: null, run: null });
}

const VERDICT_TONE: Record<string, string> = {
  survived: "green", clean: "green", consistent: "green", passed: "green", red: "red",
  failed: "red", killed: "red", findings: "amber", inconsistent: "amber",
};

function secs(a: string | null, b: string | null): string {
  if (!a || !b) return "";
  const s = Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 1000));
  if (!Number.isFinite(s)) return "";
  return s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

/** "Beacon · Site Reliability Engineer", "Seed Owner · person", "BOSS's deploy, for Seed Owner". */
export function whoWords(w: ResponderWho | null): { name: string; role: string } {
  if (!w) return { name: "Nobody yet", role: "" };
  if (w.kind === "person") return { name: w.name ?? w.role ?? "A person", role: w.name ? "person" : "" };
  if (w.kind === "agent" && w.roleKey) {
    const f = nameOf(w.roleKey);
    return { name: f.callsign || f.name, role: w.role || f.role };
  }
  return { name: w.role || "BOSS", role: w.onBehalfOf ? `for ${w.onBehalfOf}` : "" };
}

export function Who({ w, testId }: { w: ResponderWho | null; testId?: string }) {
  const { name, role } = whoWords(w);
  const face = w?.kind === "agent" ? (
    <RunFace roleKey={w.roleKey} size={24} />
  ) : w?.kind === "person" ? <RunFace roleKey={null} variant="person" size={24} /> : <RunFace roleKey={null} variant="boss" size={24} />;
  const inner = (
    <>
      {face}
      <span className="obs-who-name">{name}</span>
      {role ? <span className="obs-who-role">{role}</span> : null}
    </>
  );
  return w?.kind === "agent" && w.roleKey ? (
    <a className="obs-who" href={agentPageHref(w.roleKey)} data-testid={testId} data-role={w.roleKey}
       title={`${name}, the ${w.role}: open its page in Agent Studio (its skills, its work)`}>{inner}</a>
  ) : (
    <span className="obs-who" data-testid={testId} data-kind={w?.kind ?? "none"}
          title={w?.kind === "boss" ? "BOSS's own machinery, run on the person's approval: no agent did this step" : undefined}>{inner}</span>
  );
}

function Crew({ crew }: { crew: CrewMember[] }) {
  return (
    <ul className="obs-crew" aria-label="The build's crew" data-testid="obs-responder-crew">
      {crew.map((c) => {
        const { name } = whoWords(c.who);
        const tone = c.verdict ? VERDICT_TONE[c.verdict] : undefined;
        return (
          <li key={c.stage} data-stage={c.stage} data-testid={`obs-crew-${c.stage}`}>
            <span className="obs-crew-stage">{c.label}</span>
            {c.who.roleKey ? (
              <a className="obs-crew-who" href={agentPageHref(c.who.roleKey)} title={`${name}, the ${c.who.role}`}>
                <RunFace roleKey={c.who.roleKey} size={16} />{name}
              </a>
            ) : <span className="obs-crew-who">{name}</span>}
            {c.verdict ? <span className="obs-chip" data-tone={tone} title="Its verdict">{c.verdict}</span> : null}
            <span className="obs-crew-found" data-copy="data" title={c.finding ?? c.job}>{c.finding ?? c.job}</span>
          </li>
        );
      })}
    </ul>
  );
}

function Tests({ t }: { t: RedGreen }) {
  const files = t.files.map((f) => f.split("/").pop()).join(", ");
  return (
    <span className="obs-line obs-redgreen" data-testid="obs-responder-tests">
      <span className="inf-muted">Regression test</span>
      <span className="inf-mono" data-copy="data">{files}</span>
      {t.red ? (
        <span className="obs-chip" data-tone={t.red.failed ? "red" : "amber"} data-testid="obs-tests-red"
              title={[t.red.word, ...t.red.failing].join("\n")}>
          {t.red.failed ? "red on the faulty release" : "did not fail on the faulty release"}
        </span>
      ) : (
        <span className="obs-chip" title="This build did not record a run on the faulty code">red not recorded</span>
      )}
      <span aria-hidden>→</span>
      <span className="obs-chip" data-tone={t.green ? "green" : t.green === false ? "red" : undefined} data-testid="obs-tests-green"
            title={t.suite || undefined}>{t.green ? "green with the fix" : t.green === false ? "failing with the fix" : "not run yet"}</span>
    </span>
  );
}

function Investigation({ r }: { r: Responder }) {
  const [book, setBook] = useState(false);
  const skills = r.who?.skills ?? [];
  return (
    <>
      {r.evidence.length ? (
        <ul className="obs-evidence" aria-label="What it read" data-testid="obs-responder-evidence">
          {r.evidence.map((e) => (
            <li key={e.kind} data-kind={e.kind}>
              <span className="obs-ev-k">{e.kind === "metrics" ? "Metrics" : e.kind === "logs" ? "Logs" : e.kind === "release" ? "Release" : e.kind}</span>
              <span data-copy="data">{e.word}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <span className="obs-line">
        {r.runbook ? (
          r.runbook.markdown ? (
            <button type="button" className="inf-link" aria-expanded={book} data-testid="obs-responder-runbook"
                    title="The runbook the firing rule names: the steps this investigation follows" onClick={() => setBook((v) => !v)}>
              Runbook: {r.runbook.title} {book ? "▾" : "▸"}
            </button>
          ) : <span className="inf-faint" data-testid="obs-responder-runbook">Runbook: {r.runbook.title}</span>
        ) : null}
        {r.who?.roleKey && skills.length ? (
          <a className="inf-link" href={agentPageHref(r.who.roleKey)} data-testid="obs-responder-skills"
             title={skills.join("\n")}>{skills.length} skills ↗</a>
        ) : null}
      </span>
      {book && r.runbook?.markdown ? <div className="obs-inc-md" data-testid="obs-responder-runbook-md"><Markdown text={r.runbook.markdown} /></div> : null}
    </>
  );
}

export function Responders({ rows, onReport, answer }: {
  rows: Responder[]; onReport?: () => void;
  /** Answering the agent's question in place (the live card has a project and an actor). */
  answer?: { projectId: string; actor: string; onDone?: () => void };
}) {
  const lt = useLocalTime();
  if (!rows.length) return null;
  return (
    <div className="obs-inc-part" data-testid="obs-incident-responders">
      <span className="obs-fact-k" title="Every part of the response: which agent, person or part of BOSS did it, and what it found">Who did what</span>
      <ol className="obs-responders">
        {rows.map((r) => {
          const took = secs(r.startedAt, r.endedAt);
          const pending = !["succeeded", "failed", "skipped"].includes(r.status);
          return (
            <li key={r.stepId} className="obs-responder" data-step={r.stepId} data-kind={r.kind} data-status={r.status}
                data-testid={`obs-responder-${r.kind}`}>
              <span className="obs-line obs-responder-head">
                <Who w={r.who} testId="obs-responder-who" />
                <span className="ag-spacer" />
                <span className="obs-responder-step">{r.label}</span>
                <span className="inf-faint inf-num" title={r.endedAt ? lt.full(r.endedAt) : undefined}>
                  {pending ? (r.status === "waiting" ? "waiting" : r.status === "running" ? "working" : "not yet") : took || (r.endedAt ? lt.hhmm(Date.parse(r.endedAt)) : "")}
                </span>
                {r.pullRequestUrl ? <a className="inf-link" href={r.pullRequestUrl} target="_blank" rel="noreferrer noopener" title={r.pullRequestUrl}>PR ↗</a> : null}
                {r.kind === "incident_report" && r.status === "succeeded" && onReport ? (
                  <button type="button" className="inf-link" onClick={onReport} title="Read the report here">View</button>
                ) : null}
              </span>
              <span className="obs-responder-job">{r.job}</span>
              {r.did || r.words ? (
                <span className="obs-responder-did">
                  {r.did ? <span className="obs-strong" data-copy="data">{r.did}</span> : null}
                  {r.words ? <q className="obs-words" data-user-text data-testid="obs-responder-words">{r.words}</q> : null}
                </span>
              ) : null}
              {r.options.length && r.alertId && r.targetId ? (
                answer ? (
                  <AnswerAgent question={r.question} options={r.options} projectId={answer.projectId} actor={answer.actor}
                               targetId={r.targetId} alertId={r.alertId} onDone={answer.onDone}
                               agent={whoWords(r.who).name} />
                ) : <span className="obs-answer-q" data-testid="obs-answer-question">{r.question}</span>
              ) : null}
              {r.kind === "investigate_incident" ? <Investigation r={r} /> : null}
              {r.crew.length ? <Crew crew={r.crew} /> : null}
              {r.tests ? <Tests t={r.tests} /> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
