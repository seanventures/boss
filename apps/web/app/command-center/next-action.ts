/** Presentation priority over the existing Command Center records; no new readiness authority. */
import { withoutIds } from "../../lib/actor-words";
import type { AttentionItem } from "../../lib/agents-live-board";
import { appNeed, shelfState, type CcApp } from "../../lib/command-center-apps";
import type { CommandCenterView } from "../../lib/command-center-view";
import { decisionPatch, runPatch } from "../agents/live-util";

export type NextAction = {
  kind: "unknown" | "blocked" | "decision" | "setup" | "stopped" | "active" | "result" | "empty";
  title: string;
  detail: string;
  label?: string;
  patch?: Record<string, string | null>;
};

const buildPatch = (id: string) => ({ area: "code_tasks", view: "apps", build: id, role: null });
const setupPatch = (item: AttentionItem) => ({ area: item.fix?.area || "setup", view: null,
  role: null, focus: item.fix?.focus ?? null });

function attentionAction(item: AttentionItem, kind: NextAction["kind"]): NextAction {
  const patch = item.decisionId ? decisionPatch(item.decisionId)
    : item.build?.runId ? buildPatch(item.build.runId)
    : item.run?.id ? runPatch(item.run.id)
    : item.fix ? setupPatch(item)
    : { area: "decisions", decision_filter: "needs_action", view: null, role: null };
  return { kind, title: item.title, detail: item.detail || item.whatToDo || "Review the record before continuing.",
    label: kind === "setup" ? item.fix?.label || "Finish setup" : "Review", patch };
}

/** The hero's one action, said in words: a record's id is never its headline (tester round 2:
 *  "Reconcile expired model invocation minv_5733948397584a8182c7"). */
export function nextAction(view: CommandCenterView | null, attention: AttentionItem[],
                           apps: CcApp[] | null, reachable: boolean, appsError?: string | null): NextAction {
  const action = pickNextAction(view, attention, apps, reachable, appsError);
  return { ...action, title: withoutIds(action.title), detail: withoutIds(action.detail) };
}

function pickNextAction(view: CommandCenterView | null, attention: AttentionItem[],
                        apps: CcApp[] | null, reachable: boolean, appsError?: string | null): NextAction {
  if (!view || !reachable || !view.projectId || !view.generatedAt) return { kind: "unknown", title: "Current work is unavailable",
    detail: "Refresh to check setup, decisions and results.", label: "Refresh" };
  if (view.budgetRefusal) return { kind: "blocked", title: view.budgetRefusal.title,
    detail: view.budgetRefusal.detail, label: "Review budget", patch: { area: "budget", view: null, role: null } };
  if (view.host.show && view.host.severity === "critical") return { kind: "blocked",
    title: view.host.headline || "The host needs attention", detail: view.host.detail || view.status.tip,
    label: "Review host", patch: { area: "infra", view: null, role: null } };
  const refused = attention.find((item) => item.kind === "budget_refused");
  if (refused) return attentionAction(refused, "blocked");
  const decision = attention.find((item) => item.kind === "approval");
  if (decision) return attentionAction(decision, "decision");
  if (view.counts.needsYou > 0) return { kind: "decision", title: "Decisions are waiting on you",
    detail: "Open Approvals to review the pending decisions.", label: "Review decisions",
    patch: { area: "decisions", decision_filter: "needs_action", view: null, role: null } };
  const setup = attention.find((item) => item.kind === "readiness");
  if (setup) return { ...attentionAction(setup, "setup"), patch: setupPatch(setup) };
  if (view.counts.setup > 0 || view.status.key === "setup") return { kind: "setup", title: "Finish setup",
    detail: view.status.tip, label: "Finish setup", patch: { area: "setup", view: null, role: null } };
  const stopped = attention.find((item) => item.kind === "failed_step" || item.kind === "gate_override");
  if (stopped) return attentionAction(stopped, "stopped");
  const halted = view.missions.find((m) => ["stopped", "stalled", "blocked"].includes(m.state));
  if (halted) return { kind: "stopped", title: halted.subject,
    detail: halted.blockedReason || halted.error || `Run ${halted.state}. Review its history before continuing.`,
    label: "Review run", patch: runPatch(halted.runId) };
  const waitingApp = apps?.find((a) => appNeed(a, attention) || shelfState(a) === "needs");
  if (waitingApp) return { kind: "decision", title: waitingApp.app, detail: "This build is waiting on you.",
    label: "Review build", patch: buildPatch(appNeed(waitingApp, attention)?.runId || waitingApp.openRunId) };
  const stoppedWords: Record<string, string> = { failed: "failed", cancelled: "stopped", blocked: "is blocked",
    budget_wait: "is waiting for budget", quiet: "has no recent progress" };
  const stoppedApp = apps?.find((a) => stoppedWords[a.latestState]);
  if (stoppedApp) return { kind: "stopped", title: stoppedApp.app,
    detail: `The current build ${stoppedWords[stoppedApp.latestState]}.`,
    label: "Review build", patch: buildPatch(stoppedApp.openRunId) };
  if (view.counts.stopped > 0 || view.counts.stalled > 0) return { kind: "stopped", title: "Review stopped work",
    detail: "Open the work records before deciding how to continue.", label: "Review work",
    patch: { area: "delivery_workflows", view: null, role: null } };
  const active = view.missions.find((m) => !["done", "stopped", "stalled", "blocked"].includes(m.state));
  if (active) return { kind: "active", title: active.subject,
    detail: active.stepLabel || active.waitingOn || active.verb || "Open the run for current progress.",
    label: "Open run", patch: runPatch(active.runId) };
  // App-level state can summarize an older PR. Only the selected attempt may supply the result.
  const activeApp = apps?.find((a) => ["queued", "building"].includes(a.latestState));
  if (activeApp) return { kind: "active", title: activeApp.app, detail: "Open the current build for its progress.",
    label: "Open build", patch: buildPatch(activeApp.openRunId) };
  if (view.counts.running > 0 || view.counts.working > 0) return { kind: "active", title: "Work is in progress",
    detail: "Open the work records for current progress.", label: "Open work",
    patch: { area: "delivery_workflows", view: null, role: null } };
  const resultApp = apps?.find((a) => ["pr_open", "merged", "deployed", "done_no_pr"].includes(a.latestState)
    && a.openRunId === a.runId);
  if (resultApp) return { kind: "result", title: resultApp.app,
    detail: "Open the delivered build to review its output and checks.", label: "Review result", patch: buildPatch(resultApp.runId) };
  const outcome = view.outcomes.find((o) => o.runId);
  if (outcome?.runId) return { kind: "result", title: outcome.text,
    detail: "Open the run to review its output and history.", label: "Review result", patch: runPatch(outcome.runId) };
  const recent = view.recent[0];
  if (recent) return { kind: recent.outcome === "succeeded" ? "result" : "stopped", title: recent.name,
    detail: recent.error || `Run ${recent.outcome}. Open its output and history.`,
    label: "Review run", patch: runPatch(recent.runId) };
  if (view.outcomes.length || (apps?.length ?? 0) > 0) return { kind: "unknown", title: "Check the latest work records",
    detail: "The current records do not identify a review destination.", label: "Refresh" };
  if (appsError) return { kind: "unknown", title: "Build results are unavailable",
    detail: "Refresh to check the latest build before starting another.", label: "Refresh" };
  if (view.status.key === "first_build" || view.status.key === "quiet") {
    // The parser defaults an absent status to quiet. Project/time fields alone are
    // not a canonical empty-work answer, and absent connection rows are not evidence.
    if (!view.status.text.trim()) return { kind: "unknown", title: "Current work is unavailable",
      detail: "Refresh to check setup, decisions and results.", label: "Refresh" };
    const connectionsKnown = ["models", "repo"].every((key) => {
      const row = view.systems.find((system) => system.key === key);
      return !!row?.state.trim() && row.state.trim().toLowerCase() !== "unknown";
    });
    if (!connectionsKnown) return { kind: "unknown", title: "Setup readiness is unknown",
      detail: "Refresh to check model and repository evidence.", label: "Refresh" };
    return { kind: "empty", title: view.today.everRan ? "Describe the next task" : "Describe your first task",
      detail: "Choose one useful change and the checks it should pass." };
  }
  return { kind: "unknown", title: view.status.text || "Check current work",
    detail: view.status.tip || "Refresh to check the current records.", label: "Refresh" };
}
