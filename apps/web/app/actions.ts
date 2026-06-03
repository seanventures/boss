"use server";

import { revalidatePath } from "next/cache";
import { client } from "../lib/client";
import { buildCommandRequest } from "../lib/commands";

export async function resolveDecisionAction(formData: FormData): Promise<void> {
  const decisionId = formData.get("decision_id");
  const action = formData.get("action");
  const note = formData.get("note");
  const delegateTo = formData.get("delegate_to");
  const externalRef = formData.get("external_ref");

  if (typeof decisionId != "string" || !decisionId) {
    throw new Error("decision_id is required");
  }
  if (
    action !== "approve" &&
    action !== "reject" &&
    action !== "revise" &&
    action !== "delegate" &&
    action !== "execute"
  ) {
    throw new Error("Unsupported decision action");
  }

  const payload = {
    actor: "executive_console",
    note:
      typeof note === "string" && note.trim()
        ? note.trim()
        : "Resolved from the BOSS executive console shell.",
    delegate_to:
      typeof delegateTo === "string" && delegateTo.trim()
        ? delegateTo.trim()
        : undefined,
    external_ref:
      typeof externalRef === "string" && externalRef.trim()
        ? externalRef.trim()
        : undefined,
  };
  if (action === "approve") {
    await client.approveDecision(decisionId, payload);
  } else if (action === "reject") {
    await client.rejectDecision(decisionId, payload);
  } else if (action === "revise") {
    await client.reviseDecision(decisionId, payload);
  } else if (action === "delegate") {
    await client.delegateDecision(decisionId, payload);
  } else {
    await client.executeDecision(decisionId, payload);
  }

  revalidatePath("/");
}

export async function submitCommandAction(formData: FormData): Promise<void> {
  const payload = buildCommandRequest(
    formData.get("command"),
    formData.get("deadline"),
  );
  await client.proposeCommand(payload);
  revalidatePath("/");
}
