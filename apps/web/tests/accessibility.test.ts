import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

type ControlFinding = {
  line: number;
  tag: string;
};

function findVisibleControlsWithoutAccessibleName(source: string): ControlFinding[] {
  const lines = source.split("\n");
  const findings: ControlFinding[] = [];
  let labelDepth = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const tagMatch = line.match(/<(input|textarea|select)\b/);
    const startsInLabel = labelDepth > 0 || line.includes("<label");

    if (tagMatch) {
      const controlLines = [line];
      let cursor = index;
      while (!controlLines.join("\n").includes(">") && cursor < lines.length - 1) {
        cursor += 1;
        controlLines.push(lines[cursor] ?? "");
      }
      const controlMarkup = controlLines.join("\n");
      const isHidden = /type=["']hidden["']/.test(controlMarkup);
      const hasAccessibleName =
        startsInLabel ||
        /aria-label=/.test(controlMarkup) ||
        /aria-labelledby=/.test(controlMarkup);

      if (!isHidden && !hasAccessibleName) {
        findings.push({ line: index + 1, tag: tagMatch[1] ?? "control" });
      }
    }

    labelDepth += (line.match(/<label\b/g) ?? []).length;
    labelDepth -= (line.match(/<\/label>/g) ?? []).length;
    labelDepth = Math.max(labelDepth, 0);
  }

  return findings;
}

describe("executive console accessibility baseline", () => {
  // Note: static source scan (labels via label/aria, landmarks, roles) + explicit aria asserts for 3x features.
  // Axe-core / full automated a11y (WCAG) or lighthouse not run here (no playwright/axe deps per current package.json; P2 future).
  // Manual: use browser devtools a11y panel + axe browser ext on / after `make dev` + seed. All added controls use label/aria-label.
  // Covers: pack install UI, selfimp forms (incl notes), toasts (role/alert), commercial panels, monitoring sections, pag lists (role=list), worker notes (labels).
  it("gives every visible form control an accessible name", () => {
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    expect(findVisibleControlsWithoutAccessibleName(pageSource)).toEqual([]);
  });

  it("provides keyboard skip navigation and a labelled main landmark", () => {
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    expect(pageSource).toContain('className="skip-link"');
    expect(pageSource).toContain('aria-labelledby="boss-console-title"');
    expect(pageSource).toContain('id="decision-inbox"');
    expect(pageSource).toContain("tabIndex={-1}");
    expect(pageSource).toContain("Software Factory evidence");
    // KPI and budget panels (recent CAN/BUD additions) have labelled regions for a11y + reachability of states UI
    expect(pageSource).toContain('aria-labelledby="kpi-heading"');
    expect(pageSource).toContain('id="kpi-heading"');
    expect(pageSource).toContain('aria-labelledby="budget-control-heading"');
    expect(pageSource).toContain('id="budget-control-heading"');
    expect(pageSource).toContain('aria-labelledby="budget-allocations-heading"');
    expect(pageSource).toContain('id="budget-allocations-heading"');
    // Commercial local surfaces polish (edition, packs manager, marketplace blocked, account connect panels) + a11y per COMPKG P2 + DoD
    expect(pageSource).toContain('aria-labelledby="installed-packs-heading"');
    expect(pageSource).toContain('id="installed-packs-heading"');
    expect(pageSource).toContain('aria-labelledby="installed-roles-heading"');
    expect(pageSource).toContain('id="installed-roles-heading"');
    expect(pageSource).toContain('aria-labelledby="edition-account-connect-heading"');
    expect(pageSource).toContain('id="edition-account-connect-heading"');
    expect(pageSource).toContain('aria-labelledby="account-connect-health-heading"');
    expect(pageSource).toContain('id="account-connect-health-heading"');
    expect(pageSource).toContain('aria-labelledby="hosted-auth-sessions-heading"');
    expect(pageSource).toContain('id="hosted-auth-sessions-heading"');
    expect(pageSource).toContain('aria-labelledby="license-entitlements-heading"');
    expect(pageSource).toContain('id="license-entitlements-heading"');
    expect(pageSource).toContain('aria-labelledby="team-invites-heading"');
    expect(pageSource).toContain('id="team-invites-heading"');
    // Monitoring/obs polish (P2 per 3x batch): health/risk/event/deadletter/provider health panels have labelled regions for a11y + reachability of states (empty/error/success)
    expect(pageSource).toContain('aria-labelledby="health-snapshots-heading"');
    expect(pageSource).toContain('id="health-snapshots-heading"');
    expect(pageSource).toContain('aria-labelledby="what-changed-heading"');
    expect(pageSource).toContain('id="what-changed-heading"');
    expect(pageSource).toContain('aria-labelledby="providerops-heading"');
    expect(pageSource).toContain('id="providerops-heading"');
    expect(pageSource).toContain('aria-labelledby="billing-portal-sessions-heading"');
    expect(pageSource).toContain('id="billing-portal-sessions-heading"');
    expect(pageSource).toContain('aria-labelledby="hosted-marketplace-catalog-heading"');
    expect(pageSource).toContain('id="hosted-marketplace-catalog-heading"');
    // Self-imp forms (SIM P2 e2e-selfimp + 3x): section landmark + forms covered in client a11y
    expect(pageSource).toContain('aria-labelledby="self-improvement-heading"');
    expect(pageSource).toContain('id="self-improvement-heading"');
    // Pack import/install UI (P3 GAP-PRD-AUDIT-001 context, even if API import not yet UI surfaced; covers install form + roles lists a11y)
    expect(pageSource).toContain('aria-labelledby="company-pack-heading"');
    expect(pageSource).toContain('id="company-pack-heading"');
    // Paginated lists a11y + worker notes (capacity/cost) + selfimp + commercial + monitoring expanded (3x features)
    expect(pageSource).toContain('role="list"');
    expect(pageSource).toContain('aria-label="Recent events list (paginated feed)"');
    expect(pageSource).toContain('aria-label="Deadletters list (paginated feed)"');
    expect(pageSource).toContain('aria-label="External resources list (paginated, limit 8)"');
    expect(pageSource).toContain('aria-label="Worker pools list (paginated, notes on capacity/cost)"');
    expect(pageSource).toContain('aria-label="Self-improvement proposals list (with form actions for canary/rollout notes)"');
    expect(pageSource).toContain('aria-label="Installed packs list (pack import/install UI)"');
    expect(pageSource).toContain('aria-label="Hosted auth sessions list (commercial paginated)"');
    expect(pageSource).toContain('aria-label="Team invites list (commercial paginated)"');
    expect(pageSource).toContain('aria-label="Health snapshots list (monitoring paginated)"');
  });

  it("client toasts + action forms have a11y (aria-live, role, labelled, testids for P2 states per ux-review + GAP-AUTO-001 + CAN/BUD + 3x toasts role/alert)", () => {
    const clientSource = readFileSync(
      resolve(__dirname, "../app/client-forms.tsx"),
      "utf8",
    );
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    // Toast container + live regions + dismiss for a11y (no silent feedback)
    // Expanded for 3x: role=alert on error toasts (in addition to status banners), aria-atomic, per task
    expect(clientSource).toContain('role="region"');
    expect(clientSource).toContain('aria-label="Status notifications"');
    expect(clientSource).toContain('aria-live="polite"');
    expect(clientSource).toContain('assertive');
    expect(clientSource).toContain('role="status"');
    expect(clientSource).toContain('role="alert"');
    expect(clientSource).toContain('aria-atomic="true"');
    expect(clientSource).toContain('aria-label="Dismiss notification"');
    expect(clientSource).toContain('className="toast-container"');

    // ActionForm wrappers integrate loading/error states + revalidate refresh (5+ actions)
    expect(clientSource).toContain('aria-busy={isPending}');
    expect(clientSource).toContain('role="status"');
    expect(clientSource).toContain('role="alert"');
    expect(clientSource).toContain('data-testid="kpi-create-form"');
    expect(clientSource).toContain('data-testid="command-submit-form"');
    expect(clientSource).toContain('data-testid="provider-create-form"');
    expect(clientSource).toContain('data-testid="self-imp-proposal-form"');
    expect(clientSource).toContain('data-testid="budget-reserve-queue-form"');
    // Expanded 3x coverage for selfimp + provider + changeset forms a11y + toasts
    expect(clientSource).toContain('data-testid="self-imp-canary-form"');
    expect(clientSource).toContain('data-testid="self-imp-rollout-form"');
    expect(clientSource).toContain('data-testid="provider-test-form"');
    expect(clientSource).toContain('data-testid="provider-sync-form"');
    expect(clientSource).toContain('data-testid="changeset-apply-form"');

    // Page now uses the wrapped forms (client feedback on key actions)
    expect(pageSource).toContain('KpiCreateForm');
    expect(pageSource).toContain('CommandSubmitForm');
    expect(pageSource).toContain('ProviderCreateForm');
    expect(pageSource).toContain('BudgetReserveQueueForm');
    expect(pageSource).toContain('SelfImprovementProposalForm');
    // 3x expanded client forms in page for selfimp/pag/monitoring related actions
    expect(pageSource).toContain('SelfImpCanaryForm');
    expect(pageSource).toContain('SelfImpRolloutForm');
    expect(pageSource).toContain('ProviderTestForm');
    expect(pageSource).toContain('ProviderSyncForm');
    expect(pageSource).toContain('ChangeSetApplyForm');
  });
});
