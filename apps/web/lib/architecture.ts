/**
 * Architecture approval, as the console reads it (engine: services/architecture_docs.py).
 *
 * An architect agent turns a run's approved PRD into an architecture document; a person
 * approves it, requests changes with a note (the architect writes the next version), or rejects
 * it. Read through `/v1/architecture-reviews`; answered through the decision endpoints every
 * approval uses, so the step's panel and the Approvals card are the same answer.
 */

export type ArchitectureDecision = {
  id: string;
  status: string;
  approved_by?: string | null;
  note?: string | null;
  actions: Array<{ action: "approve" | "reject" | "revise" | "delegate" | "execute"; label: string;
                   tone: "approve" | "reject" | "neutral"; effect: string;
                   needs?: "note" | "delegate_to" | null }>;
};

export type ArchitectureVersion = {
  artifact_id: string;
  version: number;
  markdown: string;
  missing_sections: string[];
  revised_for?: string | null;
  created_at?: string | null;
};

export type ArchitectureReview = {
  id: string;
  workflow_run_id: string;
  step_id: string;
  status: "drafting" | "awaiting_approval" | "revision_requested" | "approved" | "rejected"
    | "cancelled" | "failed" | string;
  status_words: string;
  architect: string;
  title?: string | null;
  current_version: number;
  approved_by?: string | null;
  approved_version?: number | null;
  notes: Array<{ version: number; note: string; by: string; at: string }>;
  error?: string | null;
  decision: ArchitectureDecision | null;
  sections: Array<{ key: string; heading: string }>;
  versions?: ArchitectureVersion[];
  markdown?: string;
};

export type ArchitectureResult = { ok: true; review: ArchitectureReview } | { ok: false; error: string };

/** One review with every version, through the console's same-origin proxy. */
export async function fetchArchitectureReview(
  reviewId: string, projectId: string, fetcher: typeof fetch = fetch,
): Promise<ArchitectureResult> {
  try {
    const r = await fetcher(
      `/v1/architecture-reviews/${encodeURIComponent(reviewId)}?project_id=${encodeURIComponent(projectId)}`,
      { cache: "no-store" },
    );
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (body as { detail?: unknown }).detail;
      return { ok: false, error: typeof detail === "string" ? detail : `HTTP ${r.status}` };
    }
    return { ok: true, review: body as ArchitectureReview };
  } catch {
    return { ok: false, error: "the control plane did not answer" };
  }
}

/** The pill for a review: short words and a tone. The sentence is `status_words`. */
export function architecturePill(review: Pick<ArchitectureReview, "status" | "current_version"
  | "approved_version">): { label: string; tone: "good" | "warn" | "bad" | "neutral" } {
  const v = review.current_version || 1;
  switch (review.status) {
    case "approved": return { label: `Approved · v${review.approved_version ?? v}`, tone: "good" };
    case "awaiting_approval": return { label: `Needs you · v${v}`, tone: "warn" };
    case "revision_requested": return { label: `Revising · v${v + 1}`, tone: "neutral" };
    case "drafting": return { label: "Architect writing", tone: "neutral" };
    case "rejected": return { label: "Rejected", tone: "bad" };
    case "cancelled": return { label: "Cancelled", tone: "neutral" };
    default: return { label: review.status || "Unknown", tone: "neutral" };
  }
}

/** True while a person can still answer the version on screen. */
export function answerable(review: Pick<ArchitectureReview, "status" | "decision">): boolean {
  return review.status === "awaiting_approval"
    && ["pending", "delegated"].includes(review.decision?.status ?? "");
}

/** The section headings a version left out, in words. */
export function missingSections(review: Pick<ArchitectureReview, "sections">,
                                version: Pick<ArchitectureVersion, "missing_sections">): string[] {
  const names = new Map(review.sections.map((s) => [s.key, s.heading]));
  return (version.missing_sections ?? []).map((k) => names.get(k) ?? k);
}

/** The architecture document an Approvals card is about, or null for any other decision. */
export function architectureDocOf(rec: Record<string, unknown>):
  { markdown: string; version: number } | null {
  if (rec.action !== "review_architecture") return null;
  const markdown = typeof rec.document_markdown === "string" ? rec.document_markdown : "";
  if (!markdown.trim()) return null;
  return { markdown, version: typeof rec.version === "number" ? rec.version : 1 };
}
