import { redirect } from "next/navigation";

import { client, PROJECT_ID, resolveProjectId } from "../../../lib/client";

type Search = Record<string, string | string[] | undefined>;

function param(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

/**
 * Browser Easy Connect return path.
 *
 * Hosted Account Connect redirects here with code+state after the operator
 * approves. We finish the link on the control plane and send them back to
 * Editions (product surface matching boss.sean.network Community → Cloud).
 */
export default async function CloudEasyConnectCallback({
  searchParams,
}: {
  searchParams?: Promise<Search>;
}) {
  const resolved = searchParams ? await searchParams : undefined;
  const projectId = resolveProjectId(param(resolved?.project_id) ?? PROJECT_ID);
  const code = param(resolved?.code);
  const state = param(resolved?.state);
  const actor = param(resolved?.actor) ?? "ceo";

  if (code && state) {
    try {
      await client.completeCloudEasyConnectByState({
        project_id: projectId,
        actor,
        code,
        state,
      });
      redirect(`/?area=enterprise&project_id=${encodeURIComponent(projectId)}&cloud_linked=1`);
    } catch {
      redirect(
        `/?area=enterprise&project_id=${encodeURIComponent(projectId)}&cloud_error=1`,
      );
    }
  }

  redirect(`/?area=enterprise&project_id=${encodeURIComponent(projectId)}`);
}
