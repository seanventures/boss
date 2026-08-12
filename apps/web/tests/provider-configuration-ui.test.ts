import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  accountFreshness,
  costByProvider,
  matchesScope,
} from "../app/provider-configuration-ui";
import type {
  CostEventFeedItem,
  ExternalResourceFeedItem,
  ProviderAccountFeedItem,
} from "../lib/providerops";

const account: ProviderAccountFeedItem = {
  id: "pa_aws",
  provider: "aws",
  accountLabel: "AWS production",
  authMethod: "access_key",
  credentialRefs: {
    access_key_id: "env:BOSS_AWS_ACCESS_KEY_ID",
    secret_access_key: "env:BOSS_AWS_SECRET_ACCESS_KEY",
  },
  connectionMetadata: {},
  scopes: ["read"],
  status: "active",
  credentialVersion: 2,
  readOnly: true,
  deployCapable: false,
  billingReadable: true,
  destructiveActionsDisabled: true,
  capabilities: ["resource.sync"],
  missingPermissions: [],
  createdBy: "ceo",
  createdAt: "2026-07-25T00:00:00Z",
  updatedAt: "2026-07-26T00:00:00Z",
};

const resource: ExternalResourceFeedItem = {
  id: "res_1",
  providerAccountId: "pa_aws",
  provider: "aws",
  resourceType: "instance",
  externalId: "i-123",
  name: "worker-1",
  state: "active",
  ownership: "managed_by_boss",
  environment: "production",
  ownerRole: "infra_steward",
  healthStatus: "healthy",
  estimatedMonthlyUsdMicros: 2_000_000,
  costConfidence: "estimated_from_price_catalog",
  lastSeenAt: "2026-07-26T00:00:00Z",
  tags: ["region:us-east-1", "service:compute"],
  metadata: {},
  updatedAt: "2026-07-26T00:00:00Z",
};

describe("provider configuration workspace", () => {
  it("classifies untested, fresh, and stale connection evidence", () => {
    expect(accountFreshness(account, "2026-07-26T12:00:00Z")).toBe("untested");
    expect(
      accountFreshness(
        { ...account, lastTestedAt: "2026-07-26T10:00:00Z" },
        "2026-07-26T12:00:00Z",
      ),
    ).toBe("fresh");
    expect(
      accountFreshness(
        { ...account, lastTestedAt: "2026-07-24T10:00:00Z" },
        "2026-07-26T12:00:00Z",
      ),
    ).toBe("stale");
  });

  it("filters inventory across provider, account, environment, region, and service", () => {
    expect(
      matchesScope(resource, {
        provider: "aws",
        account: "pa_aws",
        environment: "production",
        region: "us-east-1",
        service: "compute",
      }),
    ).toBe(true);
    expect(
      matchesScope(resource, {
        provider: "aws",
        account: "",
        environment: "",
        region: "eu-west-1",
        service: "",
      }),
    ).toBe(false);
  });

  it("groups observed costs without changing confidence-bearing source rows", () => {
    const costs: CostEventFeedItem[] = [
      {
        id: "cost_1",
        providerAccountId: "pa_aws",
        provider: "aws",
        category: "compute",
        amountUsdMicros: 2_000_000,
        costLayer: "observed",
        confidence: "provider_reported",
        createdAt: "2026-07-26T00:00:00Z",
      },
      {
        id: "cost_2",
        providerAccountId: "pa_aws",
        provider: "aws",
        category: "storage",
        amountUsdMicros: 500_000,
        costLayer: "estimated",
        confidence: "estimated_from_price_catalog",
        createdAt: "2026-07-26T01:00:00Z",
      },
      {
        id: "cost_3",
        provider: "digitalocean",
        category: "compute",
        amountUsdMicros: 1_000_000,
        costLayer: "observed",
        confidence: "provider_reported",
        createdAt: "2026-07-26T02:00:00Z",
      },
    ];

    expect(costByProvider(costs)).toEqual([
      { provider: "aws", amount: 2_500_000 },
      { provider: "digitalocean", amount: 1_000_000 },
    ]);
    expect(costs[1].confidence).toBe("estimated_from_price_catalog");
  });

  it("keeps credentials masked and exposes the complete operator-state contract", () => {
    const source = readFileSync(
      resolve(__dirname, "../app/provider-configuration-ui.tsx"),
      "utf8",
    );

    expect(source).toContain('type="password"');
    expect(source).toContain("•••••• configured");
    expect(source).not.toContain("{account.credentialRef}");
    expect(source).not.toContain("{account.credentialRefs[");
    expect(source).toContain("Partial data (secondary APIs):");
    expect(source).toContain("bring their own");
    expect(source).toContain("ChangeSets");
    expect(source).toContain("Read-only view.");
    expect(source).toContain('status === "revoked"');
    expect(source).toContain('status === "disabled"');
    expect(source).toContain("accountFreshness");
    expect(source).toContain('role="dialog"');
    expect(source).toContain('aria-label="Saved resource scope filters"');
    expect(source).toContain("boss.providerops.scope");
    expect(source).toContain("Connections");
    expect(source).toContain("Scaling & Health");
    expect(source).toContain("Cloud FinOps");
    expect(source).toContain("ResourceReceipt");
    expect(source).toContain("editionLimitations");
    expect(source).toContain("fully functional when operators bring their own");
  });
});
