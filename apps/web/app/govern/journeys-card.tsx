"use client";

/**
 * Govern › More › Journeys (BOSSAPP-1284/1285): the preview's golden journeys today, and the
 * consecutive green days the feature freeze waits for.
 *
 *   ✓ Golden journeys                          [ 3 of 5 green days ]   [▶ Run now]
 *   Daily 06:00 Europe/London · 2 pending · 1 red
 *   ▪▪▪▪▪▪▪▪▪▪▪▪▪▪   (the last 14 days, green or not, each with why on hover)
 *   J1  Ticket to pull request …           laptop ✓ Green   boss-1 ✓ Green
 *   J2  … on an Enterprise Server repo     ⏳ Pending: PR #330
 *
 * A journey is green only when the Acceptance Agent's verdict accepted it. A pending journey
 * (its feature is still in an open PR) keeps every day red, as the freeze rule says.
 */
import React, { useState } from "react";
import {
  CalendarCheck, CheckCircle, HourglassMedium, ListChecks, MinusCircle, Play, XCircle, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { usePolledRead } from "../../lib/log-stream";
import {
  STATUS_WORDS, counterWords, polledJourneys, recordedWords, runJourneysNow, todayShort,
  type JourneyStatus, type JourneysView,
} from "../../lib/golden-journeys";
import { showToast } from "../client-forms";

const ICON: Record<JourneyStatus, Icon> = {
  green: CheckCircle, red: XCircle, pending: HourglassMedium, not_run: MinusCircle,
};

export function JourneyChip({ status, host, why, by, posted }: {
  status: JourneyStatus; host?: string; why?: string; by?: string; posted?: boolean;
}) {
  const I = ICON[status];
  const tip = [why || STATUS_WORDS[status], by].filter(Boolean).join(" · ");
  return (
    <span className="gv-jr-chip" data-status={status} data-posted={posted || undefined} title={tip}
          data-testid="gv-jr-chip">
      <I size={13} weight="bold" aria-hidden />
      {host ? <span className="gv-jr-host">{host}</span> : null}
      {STATUS_WORDS[status]}
    </span>
  );
}

export function JourneysCard({ paused }: { projectId?: string; paused: boolean }) {
  const read = usePolledRead<JourneysView>({
    key: "golden-journeys", pollMs: 60_000, paused, read: polledJourneys(),
  });
  const [busy, setBusy] = useState(false);
  const v = read.data;

  async function runNow() {
    if (busy) return;
    setBusy(true);
    const out = await runJourneysNow();
    setBusy(false);
    showToast(out.ok ? "success" : "error",
              out.ok ? `Started ${out.started.length} journeys${out.note ? ` (${out.note})` : ""}` : out.error);
    if (out.ok) read.reload();
  }

  if (!v) {
    return read.error
      ? <p className="gv-muted" role="alert" data-testid="gv-jr-error">{read.error}</p>
      : <div className="gv-skel gv-skel-short" aria-busy="true" />;
  }
  const single = v.hosts.length === 1;
  return (
    <section className="gv-card gv-jr" aria-labelledby="gv-jr-h" data-testid="gv-journeys">
      <header className="gv-jr-head">
        <span className="gv-jr-icon" aria-hidden><ListChecks size={18} weight="bold" /></span>
        <h3 id="gv-jr-h" className="gv-card-title">Golden journeys</h3>
        <span className="gv-jr-counter" data-lifted={v.freeze_lifted || undefined}
              title="Consecutive days every journey was green on every host. The freeze lifts at 5."
              data-testid="gv-jr-counter">{counterWords(v)}</span>
        <span className="gv-spacer" />
        <GatedButton permission="assurance:run" className="ag-btn ag-btn-primary"
                     disabled={busy || v.running} aria-busy={busy || undefined}
                     title={v.running ? "A run is going" : "Run every journey now"}
                     onClick={() => void runNow()} data-testid="gv-jr-run">
          <Play size={14} weight="bold" aria-hidden /> {v.running ? "Running" : "Run now"}
        </GatedButton>
      </header>
      <p className="gv-jr-sub">
        <CalendarCheck size={14} aria-hidden />
        <span data-testid="gv-jr-schedule">
          {v.schedule ? `Daily ${v.schedule.at_time} ${v.schedule.timezone}` : "Not scheduled"}
        </span>
        <span className="gv-muted">· {todayShort(v)}</span>
      </p>
      <ol className="gv-jr-days" aria-label="The last 14 days" data-testid="gv-jr-days">
        {v.history.map((d) => (
          <li key={d.day} data-green={d.green || undefined}
              title={`${d.day}: ${d.green ? "green" : d.short.slice(0, 4).join(", ") || "red"}`} />
        ))}
      </ol>
      <ul className="gv-list gv-jr-list">
        {v.journeys.map((j) => (
          <li key={j.id} data-testid={`gv-jr-${j.id}`}>
            <span className="gv-jr-name" title={j.title}><b>{j.id}</b> {j.title}</span>
            {j.pending ? (
              <JourneyChip status="pending" why={`${j.pending.waits_for}: ${j.pending.why}`} />
            ) : (
              <span className="gv-jr-hosts">
                {Object.entries(j.today).map(([host, t]) => (
                  <JourneyChip key={host} status={t.status} host={single ? undefined : host} why={t.why}
                               by={recordedWords(t)} posted={t.source === "posted"} />
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
