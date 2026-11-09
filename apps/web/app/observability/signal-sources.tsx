"use client";

/**
 * Observability › Sources › SIGNAL SOURCES (BOSSAPP-598): everything that sends BOSS metrics or
 * alerts, OTLP, remote write, webhooks from other tools, heartbeats, and whether it is getting
 * through. It lives with the stack's sources (one surface: where telemetry comes from); who is
 * told is Notify's job.
 *
 *   ● checkout-api  OpenTelemetry, prod   last 07:00, 1,204 in / 3 refused, 24h   Rotate  Test
 *                   Last refusal 06:58: "the signature did not match"
 *
 * Rotate shows the new token once; the old one keeps working for a short overlap, said in words.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { sourcePath, useSignalSources, type Source } from "../../lib/monitoring";
import { useLocalTime } from "../../lib/local-time";
import { showToast } from "../client-forms";
import { CopyButton } from "../infra/infra-util";
import { Empty, Sheet, Skeleton, ZoneHead } from "../infra/kit";
import { whenWord } from "./alerts-tab";
import { obsWrite } from "./obs-write";

function SourceLine({ s, projectId, actor, onDone, onToken }: {
  s: Source; projectId: string; actor: string; onDone: () => void; onToken: (t: { name: string; token: string; word: string }) => void;
}) {
  const lt = useLocalTime();
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const rotate = async () => {
    if (!sure) { setSure(true); return; }
    setBusy("rotate");
    setSure(false);
    const r = await obsWrite<{ token?: string; word?: string }>(sourcePath(s.id, "rotate"), { project_id: projectId, actor },
                                                                { scope: `obs:src-rotate:${s.id}`, label: "Rotate" });
    setBusy(null);
    if (r.ok && r.body?.token) onToken({ name: s.name, token: r.body.token, word: r.body.word ?? "Rotated" });
    else if (r.ok) showToast("success", r.body?.word ?? "Rotated");
    else showToast("error", r.error ?? "Not rotated");
    onDone();
  };
  const test = async () => {
    setBusy("test");
    const r = await obsWrite<{ word?: string }>(sourcePath(s.id, "test"), { project_id: projectId, actor },
                                                { scope: `obs:src-test:${s.id}`, label: "Test" });
    setBusy(null);
    showToast(r.ok ? "success" : "error", r.ok ? (r.body?.word ?? "Test sent") : (r.error ?? "Test failed"));
    onDone();
  };
  return (
    <li className="obs-zone obs-srcline" data-testid={`obs-sigsrc-${s.id}`}>
      <span className="obs-line">
        <span className="obs-adot" data-tone={!s.enabled ? "grey" : s.connected ? "green" : "amber"} aria-hidden />
        <span className="obs-strong" data-user-text>{s.name}</span>
        <span className="inf-muted">{s.kind}{s.environment ? `, ${s.environment}` : ""}</span>
        <span className="inf-faint" title={s.lastSeenAt ? lt.full(s.lastSeenAt) : "Nothing has arrived yet"}>
          {s.lastSeenAt ? `last ${whenWord(s.lastSeenAt, lt)}` : "nothing yet"}
        </span>
        <span className="inf-faint inf-num" title="Accepted / refused in the last 24 hours">
          {s.accepted24h.toLocaleString("en-US")} in{s.refused24h ? ` / ${s.refused24h.toLocaleString("en-US")} refused` : ""}, 24h
        </span>
        {!s.connected && s.enabled ? <span className="obs-chip" data-tone="amber" title="Nothing has arrived recently">Not connected</span> : null}
        {!s.enabled ? <span className="obs-chip">Off</span> : null}
        {s.oldTokenValidUntil ? (
          <span className="obs-chip" title={`The previous token works until ${lt.full(s.oldTokenValidUntil)}`}>
            old token until {whenWord(s.oldTokenValidUntil, lt)}
          </span>
        ) : null}
        <span className="ag-spacer" />
        <GatedButton permission="observability:manage" type="button" className={sure ? "ag-btn ag-btn-sm" : "inf-link"}
                     data-testid="obs-sigsrc-rotate" disabled={busy !== null}
                     title={sure ? "A new token, shown once; the old one stops after a short overlap" : s.tokenHint ? `Token …${s.tokenHint}` : "A new token, shown once"}
                     onClick={rotate}>{busy === "rotate" ? "Rotating…" : sure ? "Confirm rotate" : "Rotate"}</GatedButton>
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-sigsrc-test"
                     disabled={busy !== null} title="Send a test alert through this source" onClick={test}>
          {busy === "test" ? "Testing…" : "Test"}
        </GatedButton>
      </span>
      {s.lastRefusalWord ? (
        <span className="inf-faint" data-testid="obs-sigsrc-refusal" data-copy="data">
          Last refusal{s.lastRefusalAt ? ` ${whenWord(s.lastRefusalAt, lt)}` : ""}: {s.lastRefusalWord}
        </span>
      ) : null}
    </li>
  );
}

export function SignalSources({ projectId, actor }: { projectId: string; actor: string }) {
  const src = useSignalSources(projectId);
  const [token, setToken] = useState<{ name: string; token: string; word: string } | null>(null);
  return (
    <section className="obs-zone" aria-labelledby="obs-sigsrc-head" data-testid="obs-sigsrcs">
      <ZoneHead id="obs-sigsrc-head" label="SIGNAL SOURCES" count={src.data?.length}
                tip="Everything that sends BOSS metrics or alerts, and whether it gets through" />
      {!src.data ? (src.error ? <Empty>Can&apos;t load signal sources</Empty> : <Skeleton testId="obs-sigsrcs-loading" />)
        : src.data.length === 0 ? <Empty testId="obs-sigsrcs-empty">Nothing connected yet: use Connect an app</Empty> : (
          <ul className="obs-list">
            {src.data.map((s) => <SourceLine key={s.id} s={s} projectId={projectId} actor={actor} onDone={src.refresh} onToken={setToken} />)}
          </ul>
        )}
      {token ? (
        <Sheet title={`New token for ${token.name}`} testId="obs-sigsrc-token" onClose={() => setToken(null)}>
          <div className="obs-rform">
            <span className="obs-chip" data-tone="amber" title="BOSS keeps only a hash">Shown once: copy it now</span>
            <span className="inf-muted" data-copy="data">{token.word}</span>
            <code className="obs-promql" data-copy="data">{token.token}</code>
            <div className="inf-actions"><span className="ag-spacer" />
              <CopyButton text={token.token} label="Copy token" testId="obs-sigsrc-token-copy" />
              <button type="button" className="ag-btn ag-btn-primary" onClick={() => setToken(null)}>Done</button></div>
          </div>
        </Sheet>
      ) : null}
    </section>
  );
}
