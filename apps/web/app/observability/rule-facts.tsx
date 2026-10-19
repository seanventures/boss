"use client";

/**
 * A rule as a query (BOSSAPP-598 feedback 3): its full PromQL, then every setting that decides
 * when it fires, each a short label with the meaning in its tooltip:
 *
 *   sum(rate(http_requests_total{…,status=~"5.."}[5m])) / … > 0.05
 *   Every 1 min · Window 5m · For 2 min · Keep firing none · Threshold > 5 · Min req 20 · ▲ SEV2 · Routes to Console
 */
import React from "react";

import { durationWord, type AlertRule, type SevInfo } from "../../lib/app-alerts";
import { SevFlag } from "./severity";

/** "1 min", "none": a rule's durations, short. */
const spanWord = (s: number | null) => (s === null ? "—" : s <= 0 ? "none" : durationWord(s));

function Fact({ k, tip, children, testId }: { k: string; tip: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="obs-fact" title={tip} data-testid={testId}>
      <dt>{k}</dt>
      <dd data-copy="data">{children}</dd>
    </div>
  );
}

/** THE RULE AS A QUERY (feedback 3): its full PromQL, then every setting that decides when it fires. */
export function RuleFacts({ r, severities }: { r: AlertRule; severities?: SevInfo[] }) {
  const f = r.facts;
  return (
    <div className="obs-rfacts">
      <code className="obs-promql" data-copy="data" data-testid="obs-rule-query" title="The PromQL BOSS evaluates">{f.expr || r.expr}</code>
      <dl className="obs-facts" data-testid="obs-rule-facts">
        <Fact k="Every" tip="How often BOSS checks it (the evaluation interval)" testId="obs-fact-every">{spanWord(f.intervalS)}</Fact>
        <Fact k="Window" tip="The range each check reads, like [5m]: the granularity" testId="obs-fact-window">{f.window ?? "—"}</Fact>
        <Fact k="For" tip="How long it must stay true before it fires" testId="obs-fact-for">{spanWord(f.forS)}</Fact>
        <Fact k="Keep firing" tip="How long it keeps firing after the breach clears" testId="obs-fact-keep">{spanWord(f.keepFiringForS)}</Fact>
        <Fact k="Threshold" tip="The comparison that makes it true" testId="obs-fact-threshold">{f.threshold ?? "—"}</Fact>
        <Fact k="Min req" tip="Fewer requests than this in the window is too little to judge" testId="obs-fact-minreq">
          {f.minRequests === null ? "—" : f.minRequests}
        </Fact>
        <Fact k="Severity" tip="How serious it is, and so how it is routed" testId="obs-fact-sev"><SevFlag sev={r.severity} catalog={severities} /></Fact>
        <Fact k="Routes to" tip={f.routingTip ?? "Who is told when it fires"} testId="obs-fact-routes">{f.routing ?? "Console"}</Fact>
        {f.datasource ? <Fact k="Data" tip="Where the series it reads are held" testId="obs-fact-data">{f.datasource}</Fact> : null}
      </dl>
    </div>
  );
}
