import { API_URL, apiUrlProblem, authHeaders, resolveProjectId } from "./client";
import { apiWrite } from "./api-write";

export type CaptainPlaybook = {
  playbookKey: string;
  name: string;
  instructions: string;
  processKey: string;
  goal?: Record<string, unknown>;
  constraints?: Record<string, unknown>;
  loop?: Record<string, unknown>;
};

export async function compileCaptainPlaybook(text: string, name?: string): Promise<CaptainPlaybook | { error: string }> {
  const misconfigured = apiUrlProblem();
  if (misconfigured) return { error: misconfigured };
  try {
    const response = await apiWrite(`${API_URL}/v1/captain/playbooks/compile`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ text, name }),
    });
    if (!response.ok) return { error: `API returned ${response.status}` };
    const body = (await response.json()) as Record<string, unknown>;
    return {
      playbookKey: String(body.playbook_key || ""),
      name: String(body.name || ""),
      instructions: String(body.instructions || text),
      processKey: String(body.process_key || "prd_to_accepted_work"),
      goal: (body.goal as Record<string, unknown>) || {},
      constraints: (body.constraints as Record<string, unknown>) || {},
      loop: (body.loop as Record<string, unknown>) || {},
    };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "Unknown API error" };
  }
}

export async function saveCaptainPlaybook(
  projectId: string,
  actor: string,
  playbook: CaptainPlaybook,
): Promise<{ ok: boolean; error?: string }> {
  const project = resolveProjectId(projectId);
  try {
    const response = await apiWrite(`${API_URL}/v1/captain/playbooks`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: project,
        actor,
        playbook_key: playbook.playbookKey,
        name: playbook.name,
        instructions: playbook.instructions,
        process_key: playbook.processKey,
        goal: playbook.goal,
        constraints: playbook.constraints,
        loop: playbook.loop,
      }),
    });
    if (!response.ok) return { ok: false, error: `API returned ${response.status}` };
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Unknown API error" };
  }
}
