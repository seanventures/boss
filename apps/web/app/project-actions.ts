"use server";

/**
 * New project (POST /v1/projects): a project in this organization, seeded like the default one
 * (agents, skills, budget policy, workflows). The API decides `project:create` and its step-up;
 * a step-up answer comes back as an identity refusal, so the form opens "Confirm it's you" and
 * sends once more (lib/call-action.ts).
 */
import { revalidatePath } from "next/cache";
import { BossApiError } from "@boss/api-client";

import { failResult, okResult, type ActionResult } from "../lib/action-result";
import { client } from "../lib/client";
import { declareAction } from "../lib/identity/guarded-action";
import { act } from "../lib/server-action";

function field(form: FormData, key: string): string {
  const v = form.get(key);
  return typeof v === "string" ? v.trim() : "";
}

export async function createProjectAction(form: FormData): Promise<ActionResult> { return act(async () => {
  const name = field(form, "name");
  if (!name) return failResult("Give the project a name, then press Create.");
  const description = field(form, "description");
  try {
    const created = await client.createProject({ name, ...(description ? { description } : {}) });
    revalidatePath("/");
    return okResult(`Project ${created.name} created`, { data: { projectId: created.id } });
  } catch (error) {
    const detail = (error as BossApiError)?.body as { detail?: { error?: string; project_id?: string | null } } | undefined;
    if (error instanceof BossApiError && error.status === 409 && detail?.detail?.error === "project_exists") {
      const existing = detail.detail.project_id;
      return failResult(existing
        ? `A project called ${existing} already exists. Open it from this menu, or choose another name.`
        : "That name is taken. Choose another.", existing ? { data: { projectId: existing } } : {});
    }
    throw error;
  }
}); }

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4; lib/identity/guarded-action.ts).
// A declaration for the console to mirror — disabled with the reason when not held — never a
// decision: the API checks every call. tests/identity-mirroring.test.ts keeps this table whole.
declareAction(createProjectAction, "project:create");
