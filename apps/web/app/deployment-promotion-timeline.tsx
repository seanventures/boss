"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import type { DeploymentPromotionFeedItem } from "../lib/providerops";


const TERMINAL = new Set(["blocked", "completed", "rolled_back", "cancelled"]);


function Phase({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <li className="feed-row">
      <div>
        <h4>{label}</h4>
        {detail ? <p className="detail">{detail}</p> : null}
      </div>
      <span className={`status-pill status-${value}`}>{value.replaceAll("_", " ")}</span>
    </li>
  );
}


export function DeploymentPromotionTimeline({
  promotions,
}: {
  promotions: DeploymentPromotionFeedItem[];
}) {
  const router = useRouter();
  const hasActive = promotions.some((promotion) => !TERMINAL.has(promotion.status));

  useEffect(() => {
    if (!hasActive) return;
    const timer = window.setInterval(() => router.refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [hasActive, router]);

  if (promotions.length === 0) {
    return (
      <p className="detail">
        No deployment promotions yet. A promotion appears here after pipeline evidence
        passes and a staging run is planned.
      </p>
    );
  }

  return (
    <div className="feed-list" aria-live={hasActive ? "polite" : "off"}>
      {promotions.slice(0, 5).map((promotion) => (
        <section className="stack-card" key={promotion.id}>
          <div className="section-heading">
            <div>
              <h4>{promotion.appName} promotion</h4>
              <p className="detail">
                {promotion.repo} · <code className="mono">{promotion.commitRef}</code>
              </p>
            </div>
            <span className={`status-pill status-${promotion.status}`}>
              {promotion.status.replaceAll("_", " ")}
            </span>
          </div>
          <ol className="feed-list" aria-label={`${promotion.appName} deployment timeline`}>
            <Phase label="Pipeline verdict" value={promotion.pipelineStatus} />
            <Phase
              label="Staging deployment"
              value={promotion.stagingRunStatus ?? "waiting"}
            />
            <Phase label="Smoke / canary gate" value={promotion.gateStatus} />
            <Phase
              label="Production deployment"
              value={promotion.productionRunStatus ?? "waiting"}
            />
            <Phase
              label="Staged rollout"
              value={promotion.rolloutStatus ?? "waiting"}
              detail={promotion.rollbackReason ?? promotion.rolloutStage}
            />
          </ol>
          {promotion.failureReason ? (
            <p className="detail">Blocked: {promotion.failureReason}</p>
          ) : null}
          {!TERMINAL.has(promotion.status) ? (
            <p className="detail">Refreshing this promotion every 5 seconds.</p>
          ) : null}
        </section>
      ))}
    </div>
  );
}
