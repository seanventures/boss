import type { ArtifactFeedItem } from "./artifacts";
import type { DecisionFeedItem } from "./decisions";

const SOFTWARE_FACTORY_ARTIFACT_TYPES = new Set([
  "diff_manifest",
  "test_report",
  "security_dependency_review",
  "review_packet",
  "release_go_no_go",
]);

export type SoftwareFactoryDecisionEvidence = {
  decisionId: string;
  taskId?: string;
  title: string;
  status: string;
  validationStatus?: string;
  releaseGoNoGoStatus?: string;
  pullRequestUrl?: string;
  reviewPacketArtifactId?: string;
  diffArtifactId?: string;
  testReportArtifactId?: string;
  securityReviewArtifactId?: string;
  releaseGoNoGoArtifactId?: string;
  localValidationReceiptId?: string;
};

export type SoftwareFactoryArtifactEvidence = {
  id: string;
  artifactType: string;
  title: string;
  createdAt: string;
};

function recommendationString(
  recommendation: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = recommendation[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function hasSoftwareFactoryEvidence(decision: DecisionFeedItem): boolean {
  const action = recommendationString(decision.recommendation, "action");
  return (
    action === "review_pull_request" ||
    Boolean(recommendationString(decision.recommendation, "test_report_artifact_id")) ||
    Boolean(
      recommendationString(decision.recommendation, "security_review_artifact_id"),
    ) ||
    Boolean(
      recommendationString(decision.recommendation, "release_go_no_go_artifact_id"),
    )
  );
}

export function extractSoftwareFactoryDecisionEvidence(
  decisions: DecisionFeedItem[],
  limit = 5,
): SoftwareFactoryDecisionEvidence[] {
  return decisions
    .filter(hasSoftwareFactoryEvidence)
    .slice(0, limit)
    .map((decision) => ({
      decisionId: decision.id,
      taskId: decision.taskId,
      title: decision.question,
      status: decision.status,
      validationStatus: recommendationString(
        decision.recommendation,
        "validation_status",
      ),
      releaseGoNoGoStatus: recommendationString(
        decision.recommendation,
        "release_go_no_go_status",
      ),
      pullRequestUrl: recommendationString(
        decision.recommendation,
        "pull_request_url",
      ),
      reviewPacketArtifactId: recommendationString(
        decision.recommendation,
        "artifact_id",
      ),
      diffArtifactId: recommendationString(decision.recommendation, "diff_artifact_id"),
      testReportArtifactId: recommendationString(
        decision.recommendation,
        "test_report_artifact_id",
      ),
      securityReviewArtifactId: recommendationString(
        decision.recommendation,
        "security_review_artifact_id",
      ),
      releaseGoNoGoArtifactId: recommendationString(
        decision.recommendation,
        "release_go_no_go_artifact_id",
      ),
      localValidationReceiptId: recommendationString(
        decision.recommendation,
        "local_validation_receipt_id",
      ),
    }));
}

export function extractSoftwareFactoryArtifactEvidence(
  artifacts: ArtifactFeedItem[],
  limit = 5,
): SoftwareFactoryArtifactEvidence[] {
  return artifacts
    .filter((artifact) => SOFTWARE_FACTORY_ARTIFACT_TYPES.has(artifact.artifactType))
    .slice(0, limit)
    .map((artifact) => ({
      id: artifact.id,
      artifactType: artifact.artifactType,
      title: artifact.title,
      createdAt: artifact.createdAt,
    }));
}
