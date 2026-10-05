"use client";

/**
 * Infra › Cloud › Spend (spec §4.8; parity P-S-20, P-S-21, P-S-25, P-S-44): 30 days of metered
 * cloud spend per provider, as bars sized by each provider's share of real `cost_events` sums,
 * the anomalies (each with `Set a cap ↗` into Govern), and the provider drawer: cost events,
 * spend by category, reconciliation runs and usage. Spend that ran on a subscription reads
 * `Included in plan`, never `$0`; an estimated price carries `est.`.
 */
import React, { useMemo } from "react";

import type { CloudViewProps } from "./cloud";
import { parseAnomalies, parseSpend, type CloudSpend as Spend } from "./cloud-rows";
import { ago, localTime, useNow } from "./infra-util";
import { Drawer, DrawerSection, Empty, StateWord, ZoneHead } from "./kit";

function Amount({ s }: { s: Spend }) {
  if (s.included) return <span title="Ran on a subscription. No metered spend.">Included in plan</span>;
  return (
    <span className="inf-num">{s.spend30d}{s.estimated ? <span className="inf-faint" title="Some prices were estimated, not billed"> est.</span> : null}</span>
  );
}

/** One share bar, drawn in SVG units (a width attribute, never an inline style). */
function ShareBar({ share, label }: { share: number; label: string }) {
  const w = Math.max(share > 0 ? 1 : 0, Math.round(share * 1000) / 10);
  return (
    <svg className="cloud-bar" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${label}: ${Math.round(share * 100)}% of spend`}>
      <rect className="cloud-bar-track" x="0" y="0" width="100" height="8" rx="2" />
      <rect className="cloud-bar-fill" x="0" y="0" width={w} height="8" rx="2" />
    </svg>
  );
}

export function CloudSpend(p: CloudViewProps) {
  const { body, url, go } = p;
  const rows = useMemo(() => body.rows.map(parseSpend), [body.rows]);
  const anomalies = useMemo(() => parseAnomalies(body.header.anomalies), [body.header.anomalies]);
  const open = url.account ? rows.find((r) => r.id === url.account) ?? null : null;

  return (
    <section className="cloud-sec" aria-labelledby="inf-cloud-spend">
      <ZoneHead id="inf-cloud-spend" label="SPEND · 30D" count={rows.length} />
      {rows.length === 0 ? <Empty testId="inf-cloud-empty">No cloud spend</Empty> : (
        <figure className="cloud-fig" data-testid="inf-spend-bars" aria-label="Spend by provider, 30 days">
          <div className="inf-table-wrap">
            <table className="inf-table" data-testid="inf-cloud-table">
              <thead><tr>
                <th scope="col">Provider</th><th scope="col">30d spend</th><th scope="col">Share</th>
                <th scope="col">Events</th><th scope="col">Anomalies</th>
              </tr></thead>
              <tbody>
                {rows.map((s) => (
                  <tr key={s.id} className="cloud-row" data-open={open?.id === s.id ? "1" : undefined} data-testid={`inf-spend-row-${s.id}`}
                      onClick={() => go({ account: s.id })}>
                    <td><button type="button" className="cloud-open" onClick={(e) => { e.stopPropagation(); go({ account: s.id }); }}>
                      <span data-copy="data">{s.provider}</span></button></td>
                    <td><Amount s={s} /></td>
                    <td className="cloud-bar-cell"><ShareBar share={s.share} label={s.provider} /></td>
                    <td className="inf-num">{s.events}</td>
                    <td className="inf-num">{s.anomalies || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </figure>
      )}
      <ZoneHead id="inf-cloud-anomalies" label="ANOMALIES" count={anomalies.length} />
      <ul className="cloud-list cloud-anomalies" data-testid="inf-spend-anomalies">
        {anomalies.length === 0 ? <li className="inf-muted">No anomalies</li> : anomalies.map((a) => (
          <li key={a.id}>
            <StateWord w={a.severity} />
            <span data-copy="data">{a.provider}</span>
            <span className="inf-trunc inf-muted" data-copy="data" title={a.summary}>{a.summary}</span>
            <span className="inf-faint">{a.status}</span>
            <a className="inf-link" href="?area=budget" title="Limits live in Govern">Set a cap ↗</a>
          </li>
        ))}
      </ul>
      {open ? <ProviderDrawer s={open} onClose={() => go({ account: null })} /> : null}
    </section>
  );
}

function ProviderDrawer({ s, onClose }: { s: Spend; onClose: () => void }) {
  const now = useNow(30_000);
  return (
    <Drawer label={`${s.provider} spend`} testId="inf-provider-drawer" onClose={onClose}
            head={<><strong data-copy="data">{s.provider}</strong><Amount s={s} /></>}>
      <DrawerSection label="BY CATEGORY" testId="inf-provider-categories">
        {s.categories.length === 0 ? <Empty>No categories</Empty> : (
          <dl className="inf-facts">
            {s.categories.map((c) => <React.Fragment key={c.name}><dt data-copy="data">{c.name}</dt><dd className="inf-num">{c.usd}</dd></React.Fragment>)}
          </dl>
        )}
      </DrawerSection>
      <DrawerSection label="COST EVENTS" testId="inf-provider-events">
        {s.costEvents.length === 0 ? <Empty>No cost events</Empty> : (
          <ul className="cloud-list">
            {s.costEvents.map((c) => (
              <li key={c.id}><span data-copy="data">{c.category}</span><span className="inf-num">{c.usd}</span>
                <span className="inf-faint" title={c.confidence}>{c.confidence === "billed" ? "" : "est."}</span>
                <span className="inf-faint" title={c.at ? localTime(c.at) : undefined}>{ago(c.at, now)}</span></li>
            ))}
          </ul>
        )}
      </DrawerSection>
      <DrawerSection label="RECONCILIATION" testId="inf-provider-reconciliation">
        {s.reconciliation.length === 0 ? <Empty>No runs yet</Empty> : (
          <ul className="cloud-list">
            {s.reconciliation.map((r) => (
              <li key={r.id}><StateWord w={r.state} />
                <span className="inf-num inf-muted">{r.resourcesSeen} resources · {r.costsSeen} costs</span>
                <span className="inf-faint">{ago(r.at, now)}</span></li>
            ))}
          </ul>
        )}
      </DrawerSection>
      <DrawerSection label="USAGE" testId="inf-provider-usage">
        <dl className="inf-facts">
          <dt>Events</dt><dd className="inf-num">{s.usage.events}</dd>
          <dt>Ledger rows</dt><dd className="inf-num">{s.usage.ledger}</dd>
          <dt>Billing mirror</dt><dd data-copy="data">{s.usage.billingMirror ?? "—"}</dd>
        </dl>
      </DrawerSection>
    </Drawer>
  );
}
