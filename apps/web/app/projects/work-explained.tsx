"use client";

/**
 * WHAT WAS DISCUSSED / WHAT WAS BUILT (operator, 2026-11-19: "per work details should display what
 * was discussed and what was implemented by the Agent — we don't need to display full chat but an
 * explanation about what was implemented").
 *
 * Two short columns, on the work drawer and the build page: the panel's decisions (never the
 * transcript) with the questions it left, and the build's 3–6 bullets — what changed (its files
 * grouped by purpose), tests, the review's outcome, follow-ups, the merge. All of it the server's
 * (engine explained.py), stored once per build: a page view never pays for it.
 */
import React, { useEffect, useState } from "react";
import { ChatsCircle, Hammer } from "@phosphor-icons/react";

import { explainedPath, parseExplained, type Explained } from "../../lib/explained";
import { PxLink } from "./kit";
import "./about-app.css";

export function WorkExplained({ explained, testid = "px-explained", link = (h: string) => h }: {
  explained: Explained | null | undefined; testid?: string; link?: (href: string) => string;
}) {
  if (!explained) return null;
  const { discussed: said, built } = explained;
  return (
    <section className="wx" data-testid={testid} aria-label="What was discussed and what was built">
      <div className="wx-grid">
        {said ? (
          <div className="wx-col" data-testid={`${testid}-discussed`}>
            <div className="wx-col-h">
              <ChatsCircle size={15} weight="duotone" aria-hidden color="var(--teal)" />
              <span>What was discussed</span>
              {said.href ? <PxLink href={link(said.href)} className="px-chip" testid={`${testid}-meeting`}>Meeting</PxLink> : null}
            </div>
            {said.decisions.length ? (
              <ul className="wx-list">{said.decisions.map((d) => <li key={d}>{d}</li>)}</ul>
            ) : null}
            {said.openQuestions.length ? (
              <div className="wx-sub" data-testid={`${testid}-asks`}>
                <b>Left for you:</b> {said.openQuestions.join(" · ")}
              </div>
            ) : null}
            {said.seats ? <div className="wx-sub">Decided by a panel of {said.seats} agents</div> : null}
          </div>
        ) : null}
        {built ? (
          <div className="wx-col" data-testid={`${testid}-built`}>
            <div className="wx-col-h">
              <Hammer size={15} weight="duotone" aria-hidden color="var(--accent)" />
              <span>What was built</span>
            </div>
            <ul className="wx-list">{built.bullets.map((b) => <li key={b}>{b}</li>)}</ul>
            {built.groups.length ? (
              <div className="wx-groups" data-testid={`${testid}-groups`}>
                {built.groups.map((g) => (
                  <span key={g.purpose} className="wx-group" title={g.files.join("\n")}>{g.purpose} <b>{g.count}</b></span>
                ))}
              </div>
            ) : null}
            {explained.later.length ? (
              <div className="wx-later" data-testid={`${testid}-later`}>
                {explained.later.map((l) => (
                  <span key={l.buildId}>Then {l.number ? `change #${l.number}` : "a follow-up"}: {l.line || l.title}</span>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

/** The build page's: read for this build (GET …/builds/{id}/explained) — once, and again when
 *  the build's status moves (a completed build's explanation is stored, so it never changes). */
export function BuildExplained({ projectId, runId, status }: { projectId: string; runId: string; status?: string }) {
  const [got, setGot] = useState<Explained | null>(null);
  useEffect(() => {
    if (!projectId || !runId || typeof fetch !== "function") return undefined;
    let stopped = false;
    setGot(null);
    (async () => {
      try {
        const r = await fetch(explainedPath(projectId, runId), { cache: "no-store" });
        if (!stopped && r && r.ok) setGot(parseExplained(await r.json()));
      } catch { /* the page reads on without it */ }
    })();
    return () => { stopped = true; };
  }, [projectId, runId, status]);
  return got ? <WorkExplained explained={got} testid="fx-explained" /> : null;
}
