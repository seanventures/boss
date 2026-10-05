import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CloudSpend } from "../app/infra/cloud-spend";
import { money } from "../app/infra/cloud-rows";
import { describeFinopsSpendHonesty } from "../lib/finops";
import {
  describeResourceMonthlyCost,
  describeSpendHonesty,
  type CostEventFeedItem,
  type ProviderHealthFeedItem,
  type ReconciliationRunFeedItem,
} from "../lib/providerops";
import { readInfraUrl, type CloudBody } from "../lib/infra-view";

function cost(
  overrides: Partial<CostEventFeedItem> &
    Pick<CostEventFeedItem, "id" | "confidence" | "amountUsdMicros">,
): CostEventFeedItem {
  return {
    provider: "cloudflare",
    category: "dns",
    costLayer: "observed",
    createdAt: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

function health(
  overrides: Partial<ProviderHealthFeedItem> = {},
): ProviderHealthFeedItem {
  return {
    id: "ph_1",
    provider: "cloudflare",
    status: "healthy",
    apiAvailable: true,
    rateLimitStatus: "ok",
    usageCapStatus: "ok",
    lastCheckedAt: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

function run(
  overrides: Partial<ReconciliationRunFeedItem> = {},
): ReconciliationRunFeedItem {
  return {
    id: "recon_1",
    provider: "cloudflare",
    status: "succeeded",
    resourcesSeen: 3,
    resourcesChanged: 0,
    costsSeen: 0,
    startedBy: "ceo",
    startedAt: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

/** Infra › Cloud › Spend, rendered with the rows the server sent (the old FinOpsView's home). */
function spend(rows: CloudBody["rows"]): string {
  const body: CloudBody = {
    view: "spend", counts: { providers: rows.length }, rows, filters: {},
    header: { anomalies: [] }, notices: [], partial: [],
  };
  return renderToStaticMarkup(
    <CloudSpend projectId="p1" actor="ceo" orgId={null} url={readInfraUrl("?area=infra&view=cloud&cloud=spend")}
                go={() => {}} query="" body={body} poll={{} as never} canWrite />,
  );
}

describe("FinOps / receipt spend honesty", () => {
  it("does not treat empty, unknown, or unavailable billing as exact $0.00", () => {
    expect(describeSpendHonesty({ costs: [] })).toMatchObject({
      kind: "empty",
      amountUsdMicros: null,
    });
    expect(describeSpendHonesty({ costs: [] }).helper).toContain("not $0.00");

    expect(
      describeSpendHonesty({
        costs: [],
        health: [
          health({
            billingStatus: "unavailable",
            billingConfidence: "unknown",
            billingMessage: "Exact billing sync is not available for cloudflare",
          }),
        ],
        runs: [run({ billingStatus: "unavailable", costsSeen: 0 })],
      }),
    ).toMatchObject({
      kind: "unavailable",
      label: "Billing unavailable",
      amountUsdMicros: null,
    });

    expect(
      describeSpendHonesty({
        costs: [cost({ id: "c1", amountUsdMicros: 0, confidence: "unknown" })],
      }),
    ).toMatchObject({
      kind: "unknown",
      amountUsdMicros: null,
    });

    const estimated = describeSpendHonesty({
      costs: [
        cost({
          id: "c2",
          amountUsdMicros: 2_000_000,
          confidence: "estimated_from_price_catalog",
        }),
      ],
    });
    expect(estimated.kind).toBe("estimated");
    expect(estimated.amountUsdMicros).toBe(2_000_000);
    expect(estimated.label).toMatch(/estimated/i);

    const observed = describeSpendHonesty({
      costs: [
        cost({
          id: "c3",
          amountUsdMicros: 1_500_000,
          confidence: "provider_reported",
        }),
        cost({
          id: "c4",
          amountUsdMicros: 500_000,
          confidence: "estimated_from_price_catalog",
        }),
      ],
    });
    expect(observed.kind).toBe("observed");
    expect(observed.amountUsdMicros).toBe(1_500_000);
    expect(observed.helper).toContain("not added as exact");
  });

  it("does not coerce unknown resource monthly cost to $0.00", () => {
    expect(
      describeResourceMonthlyCost({
        costConfidence: "unknown",
      }),
    ).toMatchObject({ kind: "unknown", amountUsdMicros: null, label: "Unknown" });
    expect(
      describeResourceMonthlyCost({
        estimatedMonthlyUsdMicros: 4_000_000,
        costConfidence: "estimated_from_price_catalog",
      }),
    ).toMatchObject({ kind: "estimated", amountUsdMicros: 4_000_000 });
  });

  it("keeps FinOps summary empty/unavailable distinct from $0.00", () => {
    expect(describeFinopsSpendHonesty(null)).toMatchObject({
      kind: "unavailable",
      amountUsdMicros: null,
    });
    expect(
      describeFinopsSpendHonesty({
        projectId: "boss-dogfood",
        days: 30,
        periodStart: "",
        periodEnd: "",
        spendByProvider: [],
        spendByDay: [],
        anomaliesOpenCount: 0,
        commitmentUtilization: [],
        topResources: [],
        apiReachable: true,
      }),
    ).toMatchObject({
      kind: "empty",
      label: "No billed cost events",
      amountUsdMicros: null,
    });
  });

  // The Cloud FinOps view (provider-configuration-ui.tsx FinOpsView) retired into Infra › Cloud ›
  // Spend; the same promise — no spend is never drawn as $0.00 — is held by rendering that.
  it("renders Cloud spend empty / included / unbilled without a fake $0.00", () => {
    const empty = spend([]);
    expect(empty).toContain("No cloud spend");
    expect(empty).not.toContain("$0.00");

    const html = spend([
      { id: "openai", provider: "openai", spend_30d_usd: "$0.00", share: 0, events: 0 },
      { id: "claude", provider: "claude", included: true, share: 0, events: 4 },
      { id: "aws", provider: "aws", spend_30d_usd: "$12.40", estimated: true, share: 1, events: 9 },
    ]);
    expect(html).not.toMatch(/\$0(\.0+)?\b/);
    expect(html).toContain("Included in plan");
    expect(html).toContain("$12.40");
    expect(html).toContain(" est.");
    // The money cell itself: a zero, or nothing, is a dash.
    expect(money("$0.00")).toBe("—");
    expect(money("$0")).toBe("—");
    expect(money(undefined)).toBe("—");
  });
});
