"use server";

import { revalidatePath } from "next/cache";
import { client } from "../lib/client";

export async function resolveDecisionAction(formData: FormData): Promise<void> {
  const decisionId = formData.get("decision_id");
  const action = formData.get("action");

  if (typeof decisionId != "string" || !decisionId) {
    throw new Error("decision_id is required");
  }
  if (action !== "approve" && action !== "reject") {
    throw new Error("Unsupported decision action");
  }

  const payload = {
    actor: "executive_console",
    note: "Resolved from the BOSS executive console shell.",
  };
  if (action === "approve") {
    await client.approveDecision(decisionId, payload);
  } else {
    await client.rejectDecision(decisionId, payload);
  }

  revalidatePath("/");
}
