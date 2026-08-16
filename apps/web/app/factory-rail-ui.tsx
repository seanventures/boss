"use client";

import { useMemo, useState } from "react";

/**
 * Factory — the pipeline in one place, with its stages visible and filterable.
 *
 * Discovery, Meetings, Code Tasks and CI/CD are four stages of one journey that were four
 * separate nav areas, so the sequence existed only in the head of whoever already knew it.
 * The rail draws it: Research → Spec → Build → Ship, each with a count, each a filter.
 *
 * Card language is borrowed from Meetings, which reads better than the Factory's own:
 *   - pills carry state, instead of dense inline text
 *   - one bold name line, so the eye lands somewhere
 *   - the body is CAPPED and scrolls inside the card
 *
 * That last point removes the expand/collapse entirely. Meetings never needed a `<details>`
 * because a capped card can be read where it sits; the Factory's click-to-open is the part
 * that felt unintuitive, and the fix is not a better toggle but no toggle.
 */

export type FactoryItem = {
  id: string;
  stage: "research" | "spec" | "build" | "ship";
  name: string;
  detail?: string;
  status?: string;
  pills?: { label: string; tone?: "accent" | "good" | "warn" | "bad" }[];
  actions?: { label: string; href: string; primary?: boolean }[];
};

const STAGES = [
  { id: "research", label: "Research", why: "Is this worth building?" },
  { id: "spec", label: "Spec", why: "What exactly are we building?" },
  { id: "build", label: "Build", why: "Agents writing and testing code" },
  { id: "ship", label: "Ship", why: "On its way to production" },
] as const;

export function FactoryRail({
  discoveries = [], meetings = [], runs = [],
}: {
  discoveries?: any[]; meetings?: any[]; runs?: any[];
}) {
  const [stage, setStage] = useState<string>("all");

  // Mapped HERE, not by the caller. The mapper lives in this "use client" module, and a server
  // component cannot call into one — the page threw a 500 doing exactly that. Passing the raw
  // feeds and shaping them inside keeps the boundary where React needs it.
  const items = useMemo(
    () => toFactoryItems(discoveries, meetings, runs),
    [discoveries, meetings, runs],
  );

  const counts = useMemo(() => {
    const n: Record<string, number> = { all: items.length };
    for (const s of STAGES) n[s.id] = items.filter((i) => i.stage === s.id).length;
    return n;
  }, [items]);

  const shown = stage === "all" ? items : items.filter((i) => i.stage === stage);
  const active = STAGES.find((s) => s.id === stage);

  return (
    <>
      <div className="fx-rail" role="tablist" aria-label="Pipeline stages">
        <button
          role="tab"
          aria-selected={stage === "all"}
          className={`fx-stage${stage === "all" ? " active" : ""}`}
          onClick={() => setStage("all")}
        >
          All <span className="fx-stage-n">{counts.all}</span>
        </button>
        {STAGES.map((s, i) => (
          <span key={s.id} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span className="fx-arrow" aria-hidden>→</span>
            <button
              role="tab"
              aria-selected={stage === s.id}
              className={`fx-stage${stage === s.id ? " active" : ""}`}
              onClick={() => setStage(s.id)}
              title={s.why}
            >
              {s.label} <span className="fx-stage-n">{counts[s.id] ?? 0}</span>
            </button>
          </span>
        ))}
      </div>

      {active ? (
        <p className="detail" style={{ margin: "-6px 0 14px" }}>{active.why}</p>
      ) : null}

      {shown.length === 0 ? (
        <div className="fx-empty">
          Nothing at this stage. Work moves left to right — start something from Command
          Center&apos;s quick actions.
        </div>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {shown.map((item) => (
            <FactoryCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </>
  );
}

function FactoryCard({ item }: { item: FactoryItem }) {
  return (
    <article className="fx-card">
      <div className="fx-card-head">
        <span className="fx-card-name">{item.name}</span>
        <span className="fx-pills">
          {(item.pills ?? []).map((p, i) => (
            <span key={i} className={`fx-pill${p.tone ? ` ${p.tone}` : ""}`}>{p.label}</span>
          ))}
        </span>
      </div>
      {item.detail ? <div className="fx-card-body">{item.detail}</div> : null}
      {item.actions?.length ? (
        <div className="fx-card-foot">
          {item.actions.map((a, i) => (
            <a
              key={i}
              className={`cap-btn${a.primary ? " primary" : ""}`}
              href={a.href}
              {...(a.href.startsWith("http")
                ? { target: "_blank", rel: "noreferrer" }
                : {})}
            >
              {a.label}
            </a>
          ))}
        </div>
      ) : null}
    </article>
  );
}

/** Map the console's existing feeds onto the four stages, newest first. */
function toFactoryItems(
  discoveries: any[],
  meetings: any[],
  runs: any[],
): FactoryItem[] {
  const items: FactoryItem[] = [];

  for (const d of discoveries ?? []) {
    items.push({
      id: d.id,
      stage: "research",
      name: d.title || d.seed || "Discovery",
      detail: d.summary || d.seed,
      pills: [{ label: d.status ?? "running", tone: toneFor(d.status) }],
    });
  }

  for (const m of meetings ?? []) {
    items.push({
      id: m.id,
      stage: "spec",
      name: (m.title || m.directive || "Panel").replace(/^PRD meeting:\s*/i, ""),
      detail: m.directive,
      pills: [
        { label: m.status ?? "running", tone: toneFor(m.status) },
        ...(m.participants?.length ? [{ label: `${m.participants.length} agents` }] : []),
      ],
    });
  }

  for (const r of runs ?? []) {
    // A run that has opened a pull request has left the build stage.
    const shipped = Boolean(r.pull_request_url);
    items.push({
      id: r.id,
      stage: shipped ? "ship" : "build",
      name: r.title || "Code task",
      detail: r.spec?.instructions,
      pills: [
        { label: r.status ?? "queued", tone: toneFor(r.status) },
        ...(r.phase ? [{ label: r.phase }] : []),
        ...(r.spec?.profile ? [{ label: r.spec.profile, tone: "accent" as const }] : []),
      ],
      actions: [
        ...(r.pull_request_url
          ? [{ label: "Pull request ↗", href: r.pull_request_url }]
          : []),
      ],
    });
  }

  return items;
}

function toneFor(status?: string): "good" | "warn" | "bad" | undefined {
  if (status === "completed" || status === "succeeded") return "good";
  if (status === "running" || status === "queued") return "warn";
  if (status === "failed" || status === "blocked" || status === "cancelled") return "bad";
  return undefined;
}
