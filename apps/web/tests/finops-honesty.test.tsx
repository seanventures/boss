import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { FinOpsView } from "../app/provider-configuration-ui";
import { describeFinopsSpendHonesty } from "../lib/finops";
import {
  describeResourceMonthlyCost,
  describeSpendHonesty,
  emptyProviderOpsFeed,
  type CostEventFeedItem,
  type ProviderHealthFeedItem,
  type ProviderOpsFeed,
  type ReconciliationRunFeedItem,
} from "../lib/providerops";

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

function feed(overrides: Partial<ProviderOpsFeed> = {}): ProviderOpsFeed {
  return {
    ...emptyProviderOpsFeed("test"),
    apiReachable: true,
    blockedReason: undefined,
    ...overrides,
  };
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

  it("renders Cloud FinOps empty/unavailable without a fake $0.00 total", () => {
    const unavailable = renderToStaticMarkup(
      <FinOpsView
        feed={feed({
          providerHealth: [
            health({
              billingStatus: "unavailable",
              billingConfidence: "unknown",
              billingMessage:
                "Exact billing sync is not available for cloudflare",
            }),
          ],
          reconciliationRuns: [
            run({
              billingStatus: "unavailable",
              billingMessage:
                "Exact billing sync is not available for cloudflare",
            }),
          ],
        })}
      />,
    );

    expect(unavailable).toContain("Billing unavailable");
    expect(unavailable).toContain("Exact billing sync is not available");
    expect(unavailable).toContain("billing unavailable");
    expect(unavailable).toContain('data-testid="provider-spend-amount">—</strong>');
    expect(unavailable).not.toMatch(
      /data-testid="provider-spend-amount">\$0\.00/,
    );
    expect(unavailable).toContain("Resource receipts prove actions");

    const empty = renderToStaticMarkup(<FinOpsView feed={feed()} />);
    expect(empty).toContain("No cost events");
    expect(empty).toContain("not $0.00");
    expect(empty).toContain('data-testid="provider-spend-amount">—</strong>');
    expect(empty).not.toMatch(/data-testid="provider-spend-amount">\$0\.00/);
  });
});
