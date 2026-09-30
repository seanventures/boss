import { describe, expect, it } from "vitest";

import type { ArtifactFeedItem } from "../lib/artifacts";
import type { DecisionFeedItem } from "../lib/decisions";
import {
  extractSoftwareFactoryArtifactEvidence,
  extractSoftwareFactoryDecisionEvidence,
} from "../lib/software-factory";

function decision(
  recommendation: Record<string, unknown>,
  overrides: Partial<DecisionFeedItem> = {},
): DecisionFeedItem {
  return {
    id: "dec_pr",
    question: "Review and approve sandbox PR",
    status: "pending",
    riskTier: 2,
    recommendation,
    options: [],
    costSummary: {},
    createdAt: "2026-06-04T00:00:00Z",
    updatedAt: "2026-06-04T00:00:00Z",
    ...overrides,
  };
}

describe("Software Factory evidence mapping", () => {
  it("extracts release evidence from PR review decisions", () => {
    const items = extractSoftwareFactoryDecisionEvidence([
      decision({
        action: "review_pull_request",
        artifact_id: "art_review",
        diff_artifact_id: "art_diff",
        test_report_artifact_id: "art_test",
        security_review_artifact_id: "art_security",
        release_go_no_go_artifact_id: "art_go",
        release_go_no_go_status: "conditional_go",
        validation_status: "passed",
        pull_request_url: "https://github.com/example-org/boss-sandbox/pull/7",
        local_validation_receipt_id: "rcpt_local",
      }),
      decision({ action: "approve_changeset" }, { id: "dec_changeset" }),
    ]);

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      decisionId: "dec_pr",
      reviewPacketArtifactId: "art_review",
      testReportArtifactId: "art_test",
      securityReviewArtifactId: "art_security",
      releaseGoNoGoArtifactId: "art_go",
      releaseGoNoGoStatus: "conditional_go",
      validationStatus: "passed",
      localValidationReceiptId: "rcpt_local",
    });
  });

  it("extracts only Software Factory evidence artifacts", () => {
    const artifacts: ArtifactFeedItem[] = [
      {
        id: "art_security",
        artifactType: "security_dependency_review",
        title: "Security review",
        createdAt: "2026-06-04T00:00:00Z",
      },
      {
        id: "art_policy",
        artifactType: "policy_report",
        title: "Policy report",
        createdAt: "2026-06-04T00:00:00Z",
      },
      {
        id: "art_go",
        artifactType: "release_go_no_go",
        title: "Release go/no-go",
        createdAt: "2026-06-04T00:01:00Z",
      },
    ];

    expect(extractSoftwareFactoryArtifactEvidence(artifacts)).toEqual([
      {
        id: "art_security",
        artifactType: "security_dependency_review",
        title: "Security review",
        createdAt: "2026-06-04T00:00:00Z",
      },
      {
        id: "art_go",
        artifactType: "release_go_no_go",
        title: "Release go/no-go",
        createdAt: "2026-06-04T00:01:00Z",
      },
    ]);
  });
});
