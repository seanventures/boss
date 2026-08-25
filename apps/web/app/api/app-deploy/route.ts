import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The app card's deployment actions, off the Server-Action path.
 *
 * "Set target" showed "Saving…" for minutes. The save itself takes milliseconds — the wait was
 * the reply: a Server Action re-renders the current route to respond, setupDeploymentAction
 * ALSO called revalidatePath("/"), and this console's code_tasks area has a measured TTFB of
 * ~115 seconds. So a one-row write cost two full page renders before the button could change
 * back.
 *
 * Same fix already applied to the host list, Research and Build. These are the last three card
 * buttons that were still paying it.
 *
 * A route handler returns JSON and nothing else. The card refetches its own row afterwards,
 * which is the only thing that actually needed to change.
 */
export const dynamic = "force-dynamic";

const OPS: Record<string, (b: Record<string, unknown>) => { path: string; body: unknown }> = {
  "set-target": (b) => ({
    path: `/v1/code-tasks/${encodeURIComponent(String(b.run_id))}/setup-deployment`,
    body: {
      actor: b.actor ?? "operator",
      host_id: String(b.host_id ?? "").trim(),
      app_name: String(b.app_name ?? "").trim(),
      app_port: String(b.app_port ?? "").trim() || undefined,
      environment: "staging",
      auto_deploy: true,
    },
  }),
  deploy: (b) => ({
    path: "/v1/deployments",
    body: {
      project_id: b.project_id,
      run_id: b.run_id,
      environment: b.environment ?? "staging",
      target_id: b.target_id || undefined,
      actor: b.actor ?? "operator",
    },
  }),
  retry: (b) => ({
    path: `/v1/delivery/deployments/${encodeURIComponent(String(b.deployment_run_id))}/redeploy`,
    body: { actor: b.actor ?? "operator" },
  }),
};

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const build = OPS[String(body.op ?? "")];
  if (!build) {
    return NextResponse.json(
      { error: `unknown op: ${String(body.op)}. Expected one of ${Object.keys(OPS).join(", ")}` },
      { status: 400 },
    );
  }
  const { path, body: payload } = build(body);
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: data?.detail ?? data?.error ?? `HTTP ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
