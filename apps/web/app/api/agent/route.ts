import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Everything the chat dock READS, off the Server Action path.
 *
 * "Loading conversation… is stuck when I click Ask for Engineering Captain."
 *
 * It was not stuck; it was queued. Every one of these reads lived in `app/actions.ts`, which is
 * `"use server"` — and Next SERIALISES Server Actions, including against `router.refresh()`.
 * The console refreshes itself on a live interval, that refresh re-renders a 6 MB page taking
 * eight to twenty seconds, and a dock opened during that window sat behind it holding
 * "Loading conversation…". The same queue is why the generic starter chips were still on
 * screen: the role's own starters were waiting in it too.
 *
 * A route handler does not queue. It is the same reason the delivery-workflow screen has one:
 * that screen refreshes while a workflow runs, and a Server Action reply re-renders the whole
 * console route.
 *
 * Reads only. Sending a message still goes through the action layer, where the write belongs.
 */
export const dynamic = "force-dynamic";

async function upstream(path: string) {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({}),
      cache: "no-store",
    });
    if (!r.ok) {
      let detail = "";
      try { detail = String((await r.json())?.detail ?? ""); } catch { /* not JSON */ }
      return NextResponse.json({ error: detail || `API returned ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const view = params.get("view") ?? "";
  const project = (params.get("project_id") ?? "").trim();
  const actor = (params.get("actor") ?? "").trim();
  const p = encodeURIComponent(project);
  const a = encodeURIComponent(actor);

  if (!project) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }

  if (view === "workspace") {
    // Both halves in one round trip, and a failure in either is not fatal: the rail can be
    // empty and the panel still has to open. Holding "Loading conversation…" because a task
    // list did not answer is the bug this file exists for, in another costume.
    const [tasks, threads] = await Promise.all([
      upstream(`/v1/tasks?project_id=${p}&limit=60`).then((r) => r.json()).catch(() => ({})),
      upstream(`/v1/agent/threads?project_id=${p}&actor=${a}&since_days=10`)
        .then((r) => r.json()).catch(() => ({})),
    ]);
    return NextResponse.json({
      tasks: (tasks?.tasks ?? tasks?.items ?? []) as unknown[],
      threads: (threads?.threads ?? []) as unknown[],
    });
  }

  if (view === "messages") {
    const thread = (params.get("thread_id") ?? "").trim();
    if (!thread) return NextResponse.json({ error: "missing thread_id" }, { status: 400 });
    return upstream(
      `/v1/agent/threads/${encodeURIComponent(thread)}/messages?project_id=${p}&actor=${a}`);
  }

  if (view === "starters") {
    const role = (params.get("role") ?? "").trim();
    if (!role) return NextResponse.json({ error: "missing role" }, { status: 400 });
    return upstream(`/v1/agent/role-starters?project_id=${p}&role=${encodeURIComponent(role)}`);
  }

  if (view === "briefing") {
    // The whole factory in one read. The page renders it AND the agent can call the same tool,
    // so the list an operator is looking at and the facts BOSS answers from cannot disagree.
    return upstream(`/v1/agent/briefing?project_id=${p}`);
  }
  if (view === "progress") {
    const thread = (params.get("thread_id") ?? "").trim();
    if (!thread) return NextResponse.json({ error: "missing thread_id" }, { status: 400 });
    const since = Math.max(0, Number(params.get("since") ?? 0) | 0);
    return upstream(
      `/v1/agent/threads/${encodeURIComponent(thread)}/progress?since=${since}`);
  }

  if (view === "task-context") {
    const task = (params.get("task_id") ?? "").trim();
    if (!task) return NextResponse.json({ error: "missing task_id" }, { status: 400 });
    const [taskRow, codeTasks] = await Promise.all([
      upstream(`/v1/tasks/${encodeURIComponent(task)}?project_id=${p}`)
        .then((r) => r.json()).catch(() => ({})),
      // exclude_internal, like every other code-task read: without it a burst of BOSS's own
      // maintenance runs fills the window and the caller sees an empty list.
      upstream(`/v1/code-tasks?project_id=${p}&limit=200&exclude_internal=true`)
        .then((r) => r.json()).catch(() => ({})),
    ]);
    const all = (codeTasks?.code_tasks ?? []) as Array<Record<string, unknown>>;
    return NextResponse.json({
      task: taskRow?.task ?? taskRow ?? null,
      code_tasks: all.filter((c) => String(c.task_id ?? "") === task),
    });
  }

  return NextResponse.json(
    { error: `unknown view '${view}'. Known: workspace, messages, starters, progress, `
             + `task-context` },
    { status: 400 },
  );
}


/**
 * The one write here, and it is the operator's own: clear THIS conversation.
 *
 * A POST rather than another `view` because it changes something, and it lives beside the reads
 * for the same reason they moved — a Server Action would queue behind the console's own
 * re-render, and a Clear button that takes twenty seconds is a Clear button nobody trusts.
 */
export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  const project = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();
  const thread = String(body.thread_id ?? "").trim();

  if (action !== "clear") {
    return NextResponse.json({ error: `unknown action '${action}'. Known: clear` },
                             { status: 400 });
  }
  if (!project || !thread) {
    return NextResponse.json({ error: "clear needs project_id and thread_id" },
                             { status: 400 });
  }
  // ONE THREAD ID. Bounded to this agent's chat by construction — there is no wildcard here
  // to get wrong, and no way to express "every conversation".
  try {
    const r = await fetch(
      `${API_URL}/v1/agent/threads/${encodeURIComponent(thread)}/clear`
      + `?project_id=${encodeURIComponent(project)}&actor=${encodeURIComponent(actor)}`,
      { method: "POST", headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      let detail = "";
      try { detail = String((await r.json())?.detail ?? ""); } catch { /* not JSON */ }
      return NextResponse.json({ error: detail || `API returned ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

