"use client";

/**
 * Infra › Cloud (spec §4.8): cloud accounts, resources, capacity, spend and changes, one
 * segmented control (`cloud=` in the URL), then one table and one drawer per sub-view.
 *
 *   [Accounts 2, Resources 3, Capacity 1, Spend 2, Changes 1]  Bring your own key   Audit 
 *   <sub-view>  table + drawer (`account=` `resource=` `pool=` `changeset=`; on Spend `account=`
 *               is the provider key)
 *
 * One ETag'd read (`GET /v1/infra/cloud?view=`, polled through usePolled every 60 s) replaces
 * the old 17-call fan-out. Read-only viewers see no write control and a `Read only` chip.
 * Money is the server's dollar string; micros never reach the screen.
 */
import React, { useEffect, useRef, useState } from "react";

import { CLOUD_VIEWS, useCloud, type CloudBody, type CloudView } from "../../lib/infra-view";
import type { Polled } from "../../lib/factory-floor";
import type { InfraTabProps } from "./infra-home";
import { PartialLine, Skeleton } from "./kit";
import { CloudAccounts } from "./cloud-accounts";
import { CloudResources, type Scope, EMPTY_SCOPE } from "./cloud-resources";
import { CloudCapacity } from "./cloud-capacity";
import { CloudSpend } from "./cloud-spend";
import { CloudChanges } from "./cloud-changes";
import "./cloud.css";

export type CloudViewProps = InfraTabProps & {
  body: CloudBody;
  poll: Polled<CloudBody>;
  /** The viewer may change cloud state (no `read_only` notice). */
  canWrite: boolean;
};

const LAST_VIEW = "boss.infra.cloud.view";
const SCOPE_KEY = "boss.infra.cloud.scope";
/** Which count each segment shows (CloudBody.counts keys). */
const COUNT_KEY: Record<CloudView, string> = {
  accounts: "accounts", resources: "resources", capacity: "pools", spend: "providers", changes: "changes",
};
const NOTICE: Record<string, { label: string; tip: string }> = {
  read_only: { label: "Read only", tip: "You lack providerops_manage" },
  byok: { label: "Bring your own key", tip: "Community connects your own provider keys" },
};

function loadScope(): Scope {
  try {
    const raw = window.localStorage.getItem(SCOPE_KEY);
    return raw ? { ...EMPTY_SCOPE, ...(JSON.parse(raw) as Partial<Scope>) } : EMPTY_SCOPE;
  } catch { return EMPTY_SCOPE; }
}

export function CloudTab(props: InfraTabProps) {
  const { projectId, url, go } = props;
  const view = url.cloud;
  const [scope, setScopeState] = useState<Scope>(EMPTY_SCOPE);
  const setScope = (s: Scope) => {
    setScopeState(s);
    try { window.localStorage.setItem(SCOPE_KEY, JSON.stringify(s)); } catch { /* storage off */ }
  };

  // The URL wins; with no `cloud=` the last sub-view this viewer used comes back (P-S-05).
  useEffect(() => {
    setScopeState(loadScope());
    try {
      const here = new URLSearchParams(window.location.search);
      // A deep link (`account=`, `changeset=` …) names its own sub-view; only a bare Cloud restores.
      if (!here.get("cloud") && !["account", "resource", "pool", "changeset"].some((k) => here.get(k))) {
        const last = window.localStorage.getItem(LAST_VIEW);
        if (last && last !== "accounts" && CLOUD_VIEWS.some((c) => c.key === last)) go({ cloud: last }, "replace");
      }
    } catch { /* storage off */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filters = view === "resources" ? scope : {};
  const poll = useCloud(projectId, view, filters);
  const counts = useRef<Record<string, number>>({});
  if (poll.data) counts.current = poll.data.counts;

  const pick = (key: CloudView) => {
    try { window.localStorage.setItem(LAST_VIEW, key); } catch { /* storage off */ }
    go({ cloud: key === "accounts" ? null : key, account: null, resource: null, pool: null, changeset: null }, "replace");
  };
  const body = poll.data;
  const canWrite = !(body?.notices ?? []).some((n) => n.key === "read_only");

  return (
    <div className="cloud" data-testid="inf-cloud">
      <div className="cloud-head">
        <span className="inf-seg" role="radiogroup" aria-label="Cloud views" data-testid="inf-cloud-seg">
          {CLOUD_VIEWS.map((c) => {
            const n = counts.current[COUNT_KEY[c.key]];
            return (
              <button key={c.key} type="button" role="radio" aria-checked={view === c.key} className="inf-seg-btn"
                      data-testid={`inf-cloud-seg-${c.key}`} onClick={() => pick(c.key)}>
                {c.label}{n ? <span className="cloud-seg-n">{n}</span> : null}
              </button>
            );
          })}
        </span>
        {(body?.notices ?? []).map((n) => {
          const known = NOTICE[n.key];
          return known ? (
            <span key={n.key} className="cloud-chip" data-kind={n.key} data-testid="inf-cloud-notice" title={n.tip || known.tip}>
              {known.label}
            </span>
          ) : null;
        })}
        <span className="ag-spacer" />
        <a className="inf-link" href="?area=ops" data-testid="inf-cloud-audit" title="Every cloud event and receipt">Audit ↗</a>
      </div>
      <PartialLine partial={body?.partial ?? []} error={body ? null : poll.error} testId="inf-cloud-partial" />
      {!body ? (poll.loading ? <Skeleton testId="inf-cloud-loading" /> : null) : (
        <SubView {...props} body={body} poll={poll} canWrite={canWrite} scope={scope} setScope={setScope} />
      )}
    </div>
  );
}

function SubView(p: CloudViewProps & { scope: Scope; setScope: (s: Scope) => void }) {
  // The body answers the view it was asked for; a stale body from the previous segment waits.
  if (p.body.view !== p.url.cloud) return <Skeleton testId="inf-cloud-loading" />;
  switch (p.url.cloud) {
    case "resources": return <CloudResources {...p} />;
    case "capacity": return <CloudCapacity {...p} />;
    case "spend": return <CloudSpend {...p} />;
    case "changes": return <CloudChanges {...p} />;
    default: return <CloudAccounts {...p} />;
  }
}
