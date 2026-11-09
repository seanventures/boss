"use client";

/**
 * THE PRD ON ITS APPROVAL CARD (BOSSAPP-929). The operator: "in the approve PRD there's no link or
 * any doc / pdf to look into the PRD before approval, we need to also give the link to the place
 * where the discussion happened and PRD generated especially the meeting panel".
 *
 * One block for every surface that asks a person to approve a PRD (Approvals, Needs you, the
 * build's PRD step, the work drawer): the PRD rendered and readable in place (with "Full page"),
 * the meeting that argued it (agenda, each agent's position, dissent — "Open meeting"), and the
 * file in the repository ("In the repo", or where the build will commit it).
 */
import React, { useState } from "react";

import { Markdown } from "./markdown";
import { prdRepoWords, type PrdRecord, type PrdRepo } from "../lib/prd-record";

const box: React.CSSProperties = {
  border: "1px solid var(--line, rgba(127,127,127,.25))", borderRadius: 8, padding: "10px 12px",
  background: "var(--panel-2, transparent)",
};
const meta: React.CSSProperties = { fontSize: "var(--fs-meta)", opacity: 0.75 };
const chip: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 4, fontSize: "var(--fs-tag)", padding: "2px 8px",
  borderRadius: 999, border: "1px solid var(--line, rgba(127,127,127,.3))", textDecoration: "none", color: "inherit",
};

/** "In the repo" (the committed file) or where the build will put it, as one chip. */
export function PrdRepoLink({ repo, testid = "prd-repo" }: { repo: PrdRepo; testid?: string }) {
  const words = prdRepoWords(repo);
  return repo.committed && repo.url ? (
    <a href={repo.url} target={repo.url.startsWith("http") ? "_blank" : undefined} rel="noreferrer" style={chip}
       data-testid={testid} title={words.detail}>{words.label} · <span className="mono">{repo.path}</span></a>
  ) : (
    <span style={{ ...chip, opacity: 0.8 }} data-testid={testid} title={words.detail}>{words.label} · <span className="mono">{repo.path}</span></span>
  );
}

export function PrdRecordView({ prd, link = (h) => h, open = false, testid = "prd-record" }: {
  prd: PrdRecord; link?: (href: string) => string; open?: boolean; testid?: string;
}) {
  const [showMeeting, setShowMeeting] = useState(false);
  const m = prd.meeting;
  return (
    <section style={{ ...box, display: "grid", gap: 8 }} data-testid={testid} aria-label={`PRD: ${prd.title}`}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
        <b style={{ fontSize: "var(--fs-row)" }}>PRD · {prd.title}</b>
        <span style={{ flex: 1 }} />
        {prd.fullPage ? <a href={link(prd.fullPage)} style={chip} data-testid={`${testid}-full-page`}>Full page</a> : null}
        {m?.href ? <a href={link(m.href)} style={chip} data-testid={`${testid}-meeting-link`}>Open meeting</a> : null}
        {prd.repo ? <PrdRepoLink repo={prd.repo} testid={`${testid}-repo`} /> : null}
      </div>
      {prd.markdown ? (
        <details open={open} data-testid={`${testid}-doc`}>
          <summary style={{ cursor: "pointer", fontSize: "var(--fs-ui)" }}>Read the PRD
            <span style={meta}> · {prd.markdown.length.toLocaleString("en-US")} characters</span></summary>
          <div style={{ maxHeight: 420, overflow: "auto", marginTop: 6, fontSize: "var(--fs-body)" }}>
            <Markdown text={prd.markdown} />
          </div>
        </details>
      ) : null}
      {m ? (
        <div data-testid={`${testid}-meeting`}>
          <button type="button" onClick={() => setShowMeeting((v) => !v)} aria-expanded={showMeeting}
                  style={{ ...chip, cursor: "pointer", background: "transparent" }} data-testid={`${testid}-meeting-toggle`}>
            How the panel got here · {m.positions.length} agent{m.positions.length === 1 ? "" : "s"}
            {m.dissent.length ? ` · ${m.dissent.length} dissent` : ""}
          </button>
          {showMeeting ? (
            <div style={{ display: "grid", gap: 8, marginTop: 8, fontSize: "var(--fs-body)" }}>
              {m.agenda.brief ? (
                <div><span style={meta}>Agenda</span><p style={{ margin: "2px 0" }}>{m.agenda.brief}</p>
                  {m.agenda.participants.length ? <span style={meta}>{m.agenda.participants.join(" · ")}</span> : null}</div>
              ) : null}
              {m.positions.length ? (
                <div><span style={meta}>Each agent's position</span>
                  <ul style={{ margin: "2px 0", paddingLeft: 18 }}>
                    {m.positions.map((p) => (
                      <li key={p.seat} data-testid={`${testid}-position-${p.seat}`}><b>{p.agent}</b>: {p.position || p.debate}
                        {p.debate && p.position ? <span style={meta}> · in debate: {p.debate}</span> : null}</li>
                    ))}
                  </ul></div>
              ) : null}
              <div><span style={meta}>Dissent</span>
                {m.dissent.length ? (
                  <ul style={{ margin: "2px 0", paddingLeft: 18 }} data-testid={`${testid}-dissent`}>
                    {m.dissent.map((d, i) => <li key={i}>{d.role ? <b>{d.role}: </b> : null}{d.position}{d.reason ? ` — ${d.reason}` : ""}</li>)}
                  </ul>
                ) : <p style={{ margin: "2px 0" }} data-testid={`${testid}-no-dissent`}>None recorded.</p>}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
