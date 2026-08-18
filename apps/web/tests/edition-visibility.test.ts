import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

/**
 * U2 Community UI honesty — edition-aware visibility.
 *
 * On BOSS_EDITION=community (edition_id community):
 * - Hide paid CTAs: Account Connect, marketplace, self-improvement, enterprise clusters
 * - Keep self-hosted SSO / identity visible
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import {
  communityNavCopy,
  editionDisplayName,
  isCommunityEdition,
  isCommunityPrimaryNavVisible,
  isCommunitySurfaceVisible,
  isPaidEdition,
  isPaidSurfaceVisible,
  normalizeEditionId,
  paidSurfaceReason,
  shouldShowPaidNavBadge,
} from "../lib/edition";
import {
  CommunityIncludedBadge,
  EditionChip,
  PaidSurfaceGate,
  PaidTierBadge,
} from "../app/edition-ui";
import ConsoleShell from "../app/ConsoleShell";

describe("edition classification (mirrors API paid surface map)", () => {
  it("treats community / oss / local as unpaid", () => {
    expect(isCommunityEdition("community")).toBe(true);
    expect(isCommunityEdition("COMMUNITY")).toBe(true);
    expect(isCommunityEdition(undefined)).toBe(true);
    expect(isCommunityEdition("")).toBe(true);
    expect(isCommunityEdition("oss")).toBe(true);
    expect(isCommunityEdition("local")).toBe(true);
    expect(isPaidEdition("community")).toBe(false);
  });

  it("treats cloud and enterprise ids as paid", () => {
    expect(isCommunityEdition("cloud")).toBe(false);
    expect(isCommunityEdition("enterprise")).toBe(false);
    expect(isCommunityEdition("enterprise_cloud")).toBe(false);
    expect(isCommunityEdition("enterprise_self_hosted")).toBe(false);
    expect(isPaidEdition("enterprise")).toBe(true);
  });

  it("normalizes empty edition to community", () => {
    expect(normalizeEditionId(null)).toBe("community");
    expect(normalizeEditionId("  Cloud ")).toBe("cloud");
    expect(editionDisplayName("community")).toBe("Community");
    expect(editionDisplayName("enterprise_self_hosted")).toBe("Enterprise");
  });
});

describe("paid vs community surface visibility", () => {
  const paidSurfaces = [
    "account_connect",
    "marketplace",
    "self_improvement",
    "enterprise_clusters",
  ] as const;

  it("hides all paid surfaces on community", () => {
    for (const surface of paidSurfaces) {
      expect(isPaidSurfaceVisible("community", surface)).toBe(false);
      expect(paidSurfaceReason(surface, "community")).toMatch(/community/i);
      expect(paidSurfaceReason(surface, "community")).toMatch(/hidden/i);
    }
  });

  it("shows paid surfaces on cloud and enterprise", () => {
    for (const surface of paidSurfaces) {
      expect(isPaidSurfaceVisible("cloud", surface)).toBe(true);
      expect(isPaidSurfaceVisible("enterprise", surface)).toBe(true);
    }
  });

  it("keeps SSO / identity community surfaces always visible", () => {
    for (const edition of ["community", "cloud", "enterprise"] as const) {
      expect(isCommunitySurfaceVisible(edition, "sso_identity")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "sso_scim")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "identity_providers")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "identity_rollout")).toBe(true);
    }
  });
});

describe("community nav honesty helpers", () => {
  it("relabels leftover Enterprise nav to SSO only — not an Enterprise product area", () => {
    const copy = communityNavCopy("enterprise");
    expect(copy.label).toBe("SSO");
    expect(copy.label).not.toMatch(/Enterprise/i);
    expect(copy.sub).toMatch(/OIDC|SAML|SCIM/i);
    expect(copy.sub).not.toMatch(/Enterprise/i);
    expect(copy.badge).toBe("SSO");
  });

  it("hides design-system-as-product from Community primary nav and keeps SSO", () => {
    expect(isCommunityPrimaryNavVisible("foundations", "community")).toBe(false);
    expect(isCommunityPrimaryNavVisible("components", "community")).toBe(false);
    expect(isCommunityPrimaryNavVisible("enterprise", "community")).toBe(true);
    expect(isCommunityPrimaryNavVisible("overview", "community")).toBe(true);
    expect(isCommunityPrimaryNavVisible("foundations", "enterprise")).toBe(true);
    expect(isCommunityPrimaryNavVisible("components", "cloud")).toBe(true);
    expect(isCommunityPrimaryNavVisible("enterprise", "enterprise")).toBe(true);
  });

  it("Community v1 primary nav is the small home cut, not the full console", () => {
    // Activity log, Reliability and QA/Evidence were dropped from the nav: two had zero rows
    // behind them (incidents, drift_reports) and the third was 75% one internal audit event.
    // They remain reachable via ?area=, so this asserts the NAV cut, not their existence.
    const shown = [
      "overview",
      "decisions",
      "code_tasks",
      "budget",
      "enterprise",
    ];
    const hidden = [
      "infra",
      "metatron",
      "ops",
      "org",
      "studio",
      "sandboxes",
      "github",
      "pipeline",
      "comms",
      "foundations",
      "components",
    ];
    for (const id of shown) {
      expect(isCommunityPrimaryNavVisible(id, "community")).toBe(true);
    }
    for (const id of hidden) {
      expect(isCommunityPrimaryNavVisible(id, "community")).toBe(false);
    }
    // Paid editions keep the full nav.
    expect(isCommunityPrimaryNavVisible("infra", "enterprise")).toBe(true);
    expect(isCommunityPrimaryNavVisible("ops", "cloud")).toBe(true);
  });

  it("softens ops/org copy and marks paid nav badges only on community", () => {
    expect(communityNavCopy("ops").sub).toMatch(/deadletters|improvement programs/i);
    expect(shouldShowPaidNavBadge("ops", "community")).toBe(true);
    expect(shouldShowPaidNavBadge("org", "community")).toBe(true);
    expect(shouldShowPaidNavBadge("enterprise", "community")).toBe(false);
    expect(shouldShowPaidNavBadge("foundations", "community")).toBe(false);
    expect(shouldShowPaidNavBadge("ops", "enterprise")).toBe(false);
  });
});

describe("PaidSurfaceGate presentational honesty", () => {
  it("replaces children with honesty card on community", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "account_connect" },
        React.createElement("button", { type: "button" }, "Link Account Connect"),
      ),
    );
    expect(html).toContain('data-testid="paid-surface-hidden-account_connect"');
    expect(html).toContain("Account Connect");
    expect(html).toMatch(/Cloud/i);
    expect(html).not.toContain("Link Account Connect");
    expect(html).toMatch(/Self-hosted SSO/i);
  });

  it("passes through children on enterprise", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "enterprise", surface: "marketplace" },
        React.createElement("button", { type: "button" }, "Sync hosted catalog"),
      ),
    );
    expect(html).toContain("Sync hosted catalog");
    expect(html).not.toContain("paid-surface-hidden");
  });

  it("renders marketplace and cluster honesty independently", () => {
    const market = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "marketplace" },
        null,
      ),
    );
    const clusters = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "enterprise_clusters" },
        null,
      ),
    );
    expect(market).toContain("paid-surface-hidden-marketplace");
    expect(clusters).toContain("paid-surface-hidden-enterprise_clusters");
    expect(clusters).toMatch(/Enterprise/i);
  });
});

describe("edition chrome badges", () => {
  it("renders community edition chip and included badge", () => {
    const chip = renderToStaticMarkup(
      React.createElement(EditionChip, { editionId: "community" }),
    );
    expect(chip).toContain('data-testid="edition-chip"');
    expect(chip).toContain("Community");

    const included = renderToStaticMarkup(React.createElement(CommunityIncludedBadge));
    expect(included).toContain('data-testid="community-included-badge"');
    expect(included).toMatch(/Community/i);

    const paid = renderToStaticMarkup(
      React.createElement(PaidTierBadge, { surface: "self_improvement" }),
    );
    expect(paid).toContain('data-testid="paid-tier-badge"');
    expect(paid).toMatch(/Enterprise/i);
  });
});

describe("ConsoleShell edition-aware nav", () => {
  const baseProps = {
    status: {
      apiReachable: true,
      budgetState: "green",
      deadletters: 0,
      pendingDecisions: 0,
    },
    scope: { project: "boss-dogfood", actor: "ceo" },
    children: React.createElement("div", null, "body"),
  };

  it("shows SSO-labeled nav, hides design-system lab, and never labels a required area Enterprise", () => {
    const html = renderToStaticMarkup(
      React.createElement(ConsoleShell, {
        ...baseProps,
        editionId: "community",
      }),
    );
    expect(html).toContain('data-edition="community"');
    expect(html).toContain('data-community="true"');
    expect(html).toContain('data-area-nav="enterprise"');
    expect(html).toContain('data-sso-visible="true"');
    expect(html).toContain('data-testid="nav-sso-badge"');
    expect(html).toContain('data-testid="edition-chip"');
    // SSO stays; leftover Enterprise product framing and design-system-as-product do not.
    expect(html).not.toContain('data-area-nav="foundations"');
    expect(html).not.toContain('data-area-nav="components"');
    expect(html).not.toContain('data-area-nav="infra"');
    expect(html).not.toContain('data-area-nav="ops"');
    expect(html).not.toContain('data-area-nav="org"');
    expect(html).toContain('data-area-nav="decisions"');
    expect(html).toContain('data-area-nav="code_tasks"');
    expect(html).toContain('data-area-nav="budget"');
    // Cut from Community on 2026-08-18 — see the reasons recorded in lib/edition.ts.
    expect(html).not.toContain('data-area-nav="work"');
    expect(html).not.toContain('data-area-nav="activity"');
    expect(html).not.toContain('data-area-nav="reliability"');
    expect(html).not.toContain('data-area-nav="reports"');
    const enterpriseNav = html.match(
      /data-area-nav="enterprise"[\s\S]*?<\/button>/,
    )?.[0] ?? "";
    expect(enterpriseNav).toMatch(/>SSO</);
    expect(enterpriseNav).not.toMatch(/Enterprise/);
    expect(enterpriseNav).not.toMatch(/Editions/);
    const sidebar = html.match(
      /aria-label="Console areas"[\s\S]*?<\/nav>/,
    )?.[0] ?? "";
    expect(sidebar).not.toMatch(/>Enterprise</);
    expect(sidebar).not.toMatch(/>Foundations</);
    expect(sidebar).not.toMatch(/>Components</);
    expect(sidebar).not.toMatch(/>System Configuration</);
    // Paid surfaces live off the v1 home cut; badges must not appear as required nav.
    expect(html).not.toContain('data-testid="nav-paid-badge-ops"');
    expect(html).not.toContain('data-testid="nav-paid-badge-org"');
  });

  it("Community GA mounts keep SSO visible and never sell Account Connect as Community", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    const help = readFileSync(resolve(__dirname, "../app/onboarding-help-card.tsx"), "utf8");
    const skills = readFileSync(resolve(__dirname, "../app/skills-import-ui.tsx"), "utf8");

    expect(page).toContain("<OnboardingHelpCard");
    expect(page).toContain("<PreviewsPanel");
    expect(page).toContain("<DecisionInbox");
    expect(page).toContain("<SkillImportForm");
    expect(page).toContain("<PackImportForm");
    expect(page).toContain("PaidSurfaceGate");
    expect(page).toContain('surface="account_connect"');
    expect(page).toContain('data-testid="cloud-easy-connect"');
    expect(page.indexOf('title="BOSS Cloud Easy Connect"')).toBeLessThan(
      page.indexOf('data-testid="cloud-easy-connect"'),
    );
    expect(page).toContain("IdentityProvidersCatalogView");
    expect(page).not.toMatch(/Account Connect is (included|available) in Community/i);
    expect(page).not.toMatch(/Community includes (hosted )?Account Connect/i);
    expect(help).not.toMatch(/Account Connect/i);
    expect(help).toMatch(/Community/);
    expect(help).toMatch(/Cloud/);
    expect(skills).toMatch(/Hosted marketplace stays\s+hidden on Community/);
    expect(isCommunitySurfaceVisible("community", "sso_identity")).toBe(true);
    expect(isPaidSurfaceVisible("community", "account_connect")).toBe(false);
  });

  it("does not badge paid nav on enterprise edition and keeps lab + editions areas", () => {
    const html = renderToStaticMarkup(
      React.createElement(ConsoleShell, {
        ...baseProps,
        editionId: "enterprise",
      }),
    );
    expect(html).toContain('data-edition="enterprise"');
    expect(html).toContain('data-community="false"');
    expect(html).not.toContain("nav-paid-badge-ops");
    expect(html).not.toContain("nav-sso-badge");
    // Default label when not community-relabeled
    expect(html).toContain("Editions &amp; SSO");
    expect(html).toContain('data-area-nav="foundations"');
    expect(html).toContain('data-area-nav="components"');
    expect(html).toContain('data-area-nav="enterprise"');
  });
});
