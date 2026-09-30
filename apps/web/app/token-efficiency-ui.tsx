"use client";

import type {
  TokenConsumption,
  TokenPoliciesFeed,
  TokenRecommendationsFeed,
  TokenScorecard,
} from "../lib/token-efficiency";

const GRADE_COLOR: Record<string, string> = {
  good: "var(--green, #3ecf8e)",
  fair: "var(--amber, #f5b544)",
  poor: "var(--red, #f2647a)",
  "n/a": "var(--muted, #8b949e)",
};

const SEV_COLOR: Record<string, string> = {
  high: "var(--red, #f2647a)",
  medium: "var(--amber, #f5b544)",
  low: "var(--muted, #8b949e)",
};

function usd(micros: number): string {
  return `$${(micros / 1e6).toFixed(2)}`;
}

function pct(ratio: number | null | undefined): string {
  if (ratio == null || Number.isNaN(ratio)) return "—";
  return `${Math.round(ratio * 100)}%`;
}

function GradeChip({ label, grade }: { label: string; grade?: string }) {
  const g = grade ?? "n/a";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 12,
        fontFamily: "var(--mono, monospace)",
      }}
    >
      <span style={{ color: "var(--muted, #8b949e)" }}>{label}</span>
      <b style={{ color: GRADE_COLOR[g] ?? GRADE_COLOR["n/a"], textTransform: "uppercase" }}>
        {g}
      </b>
    </span>
  );
}

export function TokenScorecardCard({ scorecard }: { scorecard: TokenScorecard }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Token efficiency scorecard</h3>
      <p className="detail">
        Deterministic grades from the llm_calls ledger — cache ratio, failed-call burn, average
        tokens per call, cost per successful call. No model spend to compute.
      </p>
      {!scorecard.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : scorecard.calls === 0 ? (
        <p className="detail">No LLM calls recorded for this project yet.</p>
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 10,
              marginTop: 8,
            }}
          >
            <div>
              <div className="detail">Calls</div>
              <b style={{ fontSize: 18 }}>{scorecard.calls}</b>
              <div className="detail" style={{ fontSize: 11 }}>
                {scorecard.okCalls} ok · {scorecard.failedCalls} fail
              </div>
            </div>
            <div>
              <div className="detail">Cache ratio</div>
              <b style={{ fontSize: 18 }}>{pct(scorecard.cacheRatio)}</b>
              <div>
                <GradeChip label="" grade={scorecard.grades.cache_ratio} />
              </div>
            </div>
            <div>
              <div className="detail">Failed burn</div>
              <b style={{ fontSize: 18 }}>{usd(scorecard.failedBurnUsdMicros)}</b>
              <div className="detail" style={{ fontSize: 11 }}>
                {pct(scorecard.failedBurnRatio)} of spend
              </div>
            </div>
            <div>
              <div className="detail">Avg tokens/call</div>
              <b style={{ fontSize: 18 }}>
                {Math.round(scorecard.avgTokensPerCall).toLocaleString("en-US")}
              </b>
              <div>
                <GradeChip label="" grade={scorecard.grades.avg_tokens} />
              </div>
            </div>
            <div>
              <div className="detail">Cost / ok call</div>
              <b style={{ fontSize: 18 }}>
                {scorecard.costPerOkCallUsdMicros != null
                  ? usd(scorecard.costPerOkCallUsdMicros)
                  : "—"}
              </b>
              <div className="detail" style={{ fontSize: 11 }}>
                total {usd(scorecard.costUsdMicros)}
              </div>
            </div>
          </div>
          {scorecard.byStage.length > 0 ? (
            <div style={{ marginTop: 12 }}>
              <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>By stage</h4>
              <div style={{ display: "grid", gap: 4 }}>
                {scorecard.byStage.slice(0, 8).map((s) => (
                  <div
                    key={s.stage}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: 12.5,
                      gap: 8,
                    }}
                  >
                    <span style={{ fontFamily: "var(--mono, monospace)" }}>{s.stage}</span>
                    <span style={{ color: "var(--muted, #8b949e)" }}>
                      {s.calls} calls · {Math.round(s.avgTokensPerCall).toLocaleString("en-US")} tok avg ·{" "}
                      {usd(s.costUsdMicros)}
                      {s.cacheRatio != null ? ` · cache ${pct(s.cacheRatio)}` : ""}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

export function TokenConsumptionCard({ consumption }: { consumption: TokenConsumption }) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Token consumption ({consumption.windowHours}h)</h3>
      <p className="detail">
        Rolling window usage vs project daily token/cost caps (scaled to the window).
      </p>
      {!consumption.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 16, marginTop: 6 }}>
            <div>
              <div className="detail">Tokens</div>
              <b>{consumption.totalTokens.toLocaleString("en-US")}</b>
              <div className="detail" style={{ fontSize: 11 }}>
                util {pct(consumption.tokenUtilization)}
              </div>
            </div>
            <div>
              <div className="detail">Spend</div>
              <b>{usd(consumption.costUsdMicros)}</b>
              <div className="detail" style={{ fontSize: 11 }}>
                util {pct(consumption.costUtilization)}
              </div>
            </div>
            <div>
              <div className="detail">Cache</div>
              <b>{pct(consumption.cacheRatio)}</b>
            </div>
          </div>
          {consumption.alerts.length > 0 ? (
            <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
              {consumption.alerts.map((a) => (
                <div
                  key={a.kind}
                  style={{
                    borderLeft: `3px solid ${SEV_COLOR[a.severity] ?? SEV_COLOR.low}`,
                    paddingLeft: 10,
                  }}
                >
                  <div
                    style={{
                      fontSize: 11,
                      fontWeight: 700,
                      textTransform: "uppercase",
                      color: SEV_COLOR[a.severity] ?? SEV_COLOR.low,
                      fontFamily: "var(--mono, monospace)",
                    }}
                  >
                    {a.severity} · {a.kind.replaceAll("_", " ")}
                  </div>
                  <p style={{ margin: "2px 0", fontSize: 13 }}>{a.finding}</p>
                  <p style={{ margin: 0, fontSize: 12, color: "var(--muted, #8b949e)" }}>
                    → {a.action}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <p className="detail" style={{ marginTop: 8 }}>
              No budget-pressure alerts in this window.
            </p>
          )}
        </>
      )}
    </article>
  );
}

export function TokenPoliciesCard({ feed }: { feed: TokenPoliciesFeed }) {
  const project = feed.policies.find((p) => p.stage === "*");
  const stages = feed.policies.filter((p) => p.stage !== "*").slice(0, 10);
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Token budget policies</h3>
      <p className="detail">
        Per-project and per-stage caps. Defaults ship with BOSS; stored overrides win.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : (
        <>
          {project ? (
            <div style={{ marginTop: 6, fontSize: 13 }}>
              <b>Project default</b>
              <div className="detail">
                {project.maxTokensPerDay != null
                  ? `${project.maxTokensPerDay.toLocaleString("en-US")} tokens/day`
                  : "no daily token cap"}
                {" · "}
                {project.maxCostUsdMicrosPerDay != null
                  ? `${usd(project.maxCostUsdMicrosPerDay)}/day`
                  : "no daily cost cap"}
                {" · alert @ "}
                {pct(project.alertAtPct)}
                {" · "}
                <span style={{ fontFamily: "var(--mono, monospace)" }}>{project.source}</span>
              </div>
            </div>
          ) : null}
          {stages.length > 0 ? (
            <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
              {stages.map((p) => (
                <div
                  key={p.stage}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 12.5,
                    gap: 8,
                  }}
                >
                  <span style={{ fontFamily: "var(--mono, monospace)" }}>{p.stage}</span>
                  <span style={{ color: "var(--muted, #8b949e)" }}>
                    {p.maxTotalTokensPerCall != null
                      ? `${p.maxTotalTokensPerCall.toLocaleString("en-US")} tok/call`
                      : "—"}
                    {p.preferredModelClass ? ` · ${p.preferredModelClass}` : ""}
                    {!p.enabled ? " · off" : ""}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

export function TokenRecommendationsCard({
  feed,
}: {
  feed: TokenRecommendationsFeed;
}) {
  return (
    <article className="stack-card">
      <h3 style={{ margin: 0 }}>Token recommendations</h3>
      <p className="detail">
        Deterministic rules extending cost insights — profile{" "}
        <span style={{ fontFamily: "var(--mono, monospace)" }}>{feed.profile}</span>.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">API unreachable.</p>
      ) : feed.recommendations.length === 0 ? (
        <p className="detail">No material findings — spend below thresholds or already efficient.</p>
      ) : (
        <div style={{ display: "grid", gap: 10, marginTop: 6 }}>
          {/* THE INDEX IS PART OF THE KEY ON PURPOSE.
              `source`+`kind` is not unique: per-stage rules emit one recommendation per stage
              under the same kind, so a project with two stages over their token cap produced
              two children keyed `token_efficiency-stage_over_token_cap`. React does not merely
              warn about that — it may drop or duplicate rows, so a real finding could silently
              vanish from this card. `stage` tells the common case apart; the index guarantees
              uniqueness for any rule that duplicates in a way we have not seen yet. Safe here
              because this list is rendered from a server array and is never reordered, edited
              or filtered in the browser. */}
          {feed.recommendations.map((rec, i) => (
            <div
              key={`${rec.source}-${rec.kind}-${rec.stage ?? ""}-${i}`}
              style={{
                borderLeft: `3px solid ${SEV_COLOR[rec.severity] ?? SEV_COLOR.low}`,
                paddingLeft: 10,
              }}
            >
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
                <span
                  style={{
                    color: SEV_COLOR[rec.severity] ?? SEV_COLOR.low,
                    fontFamily: "var(--mono, monospace)",
                    fontSize: 11,
                    fontWeight: 700,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                  }}
                >
                  {rec.severity} · {rec.kind.replaceAll("_", " ")}
                </span>
                <span className="detail" style={{ fontSize: 11 }}>
                  {rec.source}
                </span>
                {rec.potentialSavingUsdMicros > 0 ? (
                  <b style={{ fontSize: 12.5 }}>save ≈ {usd(rec.potentialSavingUsdMicros)}</b>
                ) : null}
                {rec.potentialTokenSaving > 0 ? (
                  <b style={{ fontSize: 12.5 }}>
                    ≈ {rec.potentialTokenSaving.toLocaleString("en-US")} tok
                  </b>
                ) : null}
              </div>
              <p style={{ margin: "3px 0 2px", fontSize: 13 }}>{rec.finding}</p>
              <p style={{ margin: 0, fontSize: 12.5, color: "var(--muted, #8b949e)" }}>
                → {rec.action}
              </p>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

/** Composite panel for the Budget area of Mission Control. */
export function TokenEfficiencyPanel({
  scorecard,
  consumption,
  policies,
  recommendations,
}: {
  scorecard: TokenScorecard;
  consumption: TokenConsumption;
  policies: TokenPoliciesFeed;
  recommendations: TokenRecommendationsFeed;
}) {
  return (
    <section className="panel-stack" data-area="budget" aria-label="Token efficiency">
      <div className="section-head">
        <div>
          <span className="eyebrow">Token Efficiency</span>
          <h2>Consumption control, scorecards, and packing tips.</h2>
        </div>
        <p className="status-meta">
          Source: `/v1/token-efficiency/*` over llm_calls — no external model calls.
        </p>
      </div>
      <div
        className="stack-grid"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}
      >
        <TokenScorecardCard scorecard={scorecard} />
        <TokenConsumptionCard consumption={consumption} />
        <TokenPoliciesCard feed={policies} />
        <TokenRecommendationsCard feed={recommendations} />
      </div>
    </section>
  );
}
