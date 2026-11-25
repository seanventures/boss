/**
 * A PIECE OF WORK'S LINKS — Workflow · Build · Change · Preview — ONE SOURCE for every Work row,
 * its iterations, the run screen's header line and the Work tab's "Latest" strip (operator 2026-11-21: "I dont see
 * any run, build, preview etc in the UI … we need to have individual run for every run that's
 * displayed in the work task … and overall run, build and change should be displayed at the
 * top whatever is latest").
 *
 * The row lost them in the Work panel rebuild (#246, a2d9d5866: "Work rows: title, one line of
 * state, a compact flow, ONE action"); they come back as compact chips with short labels — a
 * raw `crun_…` id is never the words, only the tooltip.
 */
import { isHydrated } from "./hydration-state";
import type { Attempt, WorkItem, WorkState } from "./project-experience";
import { changeHref, changeRef, isLocalChangeUrl } from "./repo-links";
import { mergeWaitOf } from "./work-next-step";

/** The build that OPENED the change (SPEC §5.2): the merge's own build, never the newest
 *  follow-up that pushed to it. Its console link keeps the row's link and swaps the id. */
export function rootBuildOf(w: Pick<WorkItem, "links" | "action_label">): { id: string; href: string } | null {
  const fromHref = new URLSearchParams(w.links.build.split("?")[1] ?? "").get("build") ?? "";
  const id = mergeWaitOf(w)?.build_id || fromHref;
  if (!id) return null;
  const href = w.links.build ? (fromHref ? w.links.build.replace(`build=${fromHref}`, `build=${id}`) : w.links.build) : "";
  return { id, href };
}

/** The change this work made: the open merge, else the PR it recorded, else the one it merged. */
export function workChangeOf(w: Pick<WorkItem, "links" | "action_label">): { number: number; external_url: string } | null {
  return mergeWaitOf(w) ?? w.links.pr ?? w.links.merged_pr ?? null;
}

/** The run id inside a console run link (`wfr=` / `run=`), for the tooltip. */
function runIdOf(href: string): string {
  const q = new URLSearchParams(href.split("?")[1] ?? "");
  return q.get("wfr") || q.get("run") || q.get("run_id") || "";
}

export type WorkLinkKey = "run" | "build" | "change" | "preview";
export type WorkLink = {
  key: WorkLinkKey;
  /** Short words: "Workflow #4", "Build", "PR #5" / "Change #5", "Preview". Never a raw id. */
  label: string;
  href: string;
  /** The id lives here, on hover. */
  title: string;
  external: boolean;
};

/** A row's links, in order Workflow · Build · Change · Preview; only the ones that exist. In
 *  words a person reads, a workflow run is a "Workflow" (design #287). */
export function workLinks(w: WorkItem): WorkLink[] {
  const out: WorkLink[] = [];
  const run = w.links.workflow_run;
  if (run) {
    const id = runIdOf(run);
    // The run itself, full page (workOpenHref): the bare `wfr=` link lands on the runs list,
    // where a finished run further down is not the one shown (link walk 2026-11-22).
    out.push({ key: "run", label: w.run_number ? `Workflow #${w.run_number}` : "Workflow", href: workOpenHref(w) || run,
               title: ["Workflow run", w.run_number ? `#${w.run_number}` : "", id ? `· ${id}` : ""].filter(Boolean).join(" "), external: false });
  }
  const build = rootBuildOf(w);
  if (build?.href) out.push({ key: "build", label: "Build", href: build.href, title: `Build · ${build.id}`, external: false });
  const ch = workChangeOf(w);
  if (ch?.number) {
    const href = changeHref(ch.external_url) || "";
    if (href) {
      out.push({ key: "change", label: changeRef(ch.external_url, ch.number), href,
                 title: `${isLocalChangeUrl(ch.external_url) ? "Change set in BOSS" : "Pull request"} #${ch.number}`,
                 external: /^https?:\/\//.test(href) });
    }
  }
  const preview = w.links.preview || (w.preview_app?.href ?? "");
  if (preview) out.push({ key: "preview", label: "Preview", href: preview, title: "The running preview", external: /^https?:\/\//.test(preview) });
  return out;
}

/** "5m ago", "3h ago", "2d ago"; "" when unknown. */
export function agoShort(iso: string | null | undefined, now?: number | null): string {
  const t = Date.parse(iso || "");
  if (!Number.isFinite(t)) return "";
  if (now === null || (now === undefined && !isHydrated())) return "";
  const ref = now ?? Date.now();
  const s = Math.max(0, Math.floor((ref - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export type LatestLink = WorkLink & {
  /** The work it belongs to, its state (the dot) and when it last moved. */
  work_id: string; work_label: string; state: WorkState; at: string; ago: string;
};

/** The project's most recent Run, Build, Change and Preview across all its work (the Latest
 *  strip): for each kind, the link of the work that moved last. */
export function latestLinks(items: WorkItem[], now?: number | null): LatestLink[] {
  const when = (w: WorkItem) => Date.parse(w.updated_at || w.created_at || "") || 0;
  const sorted = [...items].sort((a, b) => when(b) - when(a));
  const out: LatestLink[] = [];
  for (const key of ["run", "build", "change", "preview"] as WorkLinkKey[]) {
    for (const w of sorted) {
      const l = workLinks(w).find((x) => x.key === key);
      if (!l) continue;
      const at = w.updated_at || w.created_at || "";
      out.push({ ...l, work_id: w.id, work_label: w.label, state: w.state, at, ago: agoShort(at, now) });
      break;
    }
  }
  return out;
}

/** Engine prose names builds by their raw id (`crun_ad50ce9f01e14534b230`); a person reads
 *  "build ad50ce9f". Shas are cut to 7. */
export function tidyIds(text: string): string {
  return text
    .replace(/\bcrun_([0-9a-f]{8})[0-9a-f]*\b/gi, "build $1")
    .replace(/\bwfr_([0-9a-z]{8})[0-9a-z]*\b/gi, "run $1")
    .replace(/\b([0-9a-f]{7})[0-9a-f]{5,33}\b/g, "$1");
}

/** WHERE A WORK ROW OPENS (design #287, decision 1): its latest Workflow run, full page, with
 *  "← Work" back (`from=work`). Work with no workflow (a build on its own) opens its Build page,
 *  a lone decision opens the decision; nothing to open → "". The Work panel this replaces is gone; old drawer links come here too. */
export function workOpenHref(w: Pick<WorkItem, "links" | "action_label">): string {
  const run = w.links.workflow_run;
  const id = run ? runIdOf(run) : "";
  if (run && id) {
    const q = new URLSearchParams(run.split("?")[1] ?? "");
    for (const k of ["drawer", "wp", "step", "view", "wf"]) q.delete(k);
    q.set("area", "delivery_workflows"); q.set("wfr", id); q.set("tab", "runs"); q.set("page", "full"); q.set("from", "work");
    return `?${q}`;
  }
  // A decision with no run or build (a merge or deploy approval on its own) opens that decision.
  return rootBuildOf(w)?.href || w.links.build || w.links.decision || "";
}

/** One iteration's links (a re-run, a repair build, a follow-up): its Build and its Change. */
export function iterationLinks(t: Pick<Attempt, "id" | "build" | "pr">): WorkLink[] {
  const out: WorkLink[] = [];
  if (t.build) out.push({ key: "build", label: "Build", href: t.build, title: `Build · ${t.id}`, external: false });
  if (t.pr?.number) {
    const href = changeHref(t.pr.external_url) || "";
    if (href) out.push({ key: "change", label: changeRef(t.pr.external_url, t.pr.number), href,
                         title: `${isLocalChangeUrl(t.pr.external_url) ? "Change set in BOSS" : "Pull request"} #${t.pr.number}`,
                         external: /^https?:\/\//.test(href) });
  }
  return out;
}

/** When it finished: the last time it moved, for work that is over (done or failed); "" while
 *  it is still going. */
export function finishedAt(w: { state: string; updated_at?: string; created_at?: string }): string {
  return w.state === "done" || w.state === "failed" || w.state === "handed_on" ? (w.updated_at || "") : "";
}

/** The piece of work a workflow run is: the item named by the run (its id, or its run link). */
export function workForRun(items: WorkItem[], runId: string): WorkItem | null {
  if (!runId) return null;
  return items.find((w) => w.id === runId) ?? items.find((w) => runIdOf(w.links.workflow_run || "") === runId) ?? null;
}
