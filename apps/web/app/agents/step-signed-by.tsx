"use client";

/**
 * A step's drawer, signed: who did this step, how it reached them, what they said (spec §3.6).
 *
 * `Signed by` is the Act card inline. `Said` is the agent's last three completion turns for this
 * work, trimmed, read from the work's own record (never a summary written in the browser). A
 * failed step shows the face stuck, with the reason one hover away. `Ask <Name>` opens the 1:1
 * talk with that agent, about this step, its replies signed under this work.
 */
import React from "react";

import type { Act } from "../../lib/agent-world-types";
import { ActCard, askAboutAct } from "./act-card";
import { whoDid } from "./chain-model";
import { useActDetail } from "./run-acts";
import { nameOf, RunFace } from "./run-face";
import { useIdentityVersion } from "../../lib/use-agent-identities";

const SAID_MAX = 280;

/** One turn, trimmed at a word, so a long answer stays a glance. */
export function trimSaid(text: string, max = SAID_MAX): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const at = cut.lastIndexOf(" ");
  return `${(at > max * 0.6 ? cut.slice(0, at) : cut).replace(/[,;:.\s]+$/, "")}…`;
}

export function StepSignedBy({ act, approval, projectId, viewer, failure, question, askless }: {
  act: Act;
  /** The person's answer to this step's gate, when it had one. */
  approval?: Act | null;
  projectId: string; viewer: string;
  /** Why the step failed, when it did. */
  failure?: string | null;
  /** What the operator is asking about this step, prefilled in the dock. */
  question: string;
  /** Leave out the Ask buttons: the step panel's Done-by line carries the one Ask. */
  askless?: boolean;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const detail = useActDetail(projectId, act.actId);
  const who = whoDid(act, viewer);
  const named = nameOf(who.roleKey, who.variant, who.personId, who.name);
  // FAILED (it errored) or STOPPED (left open when its run stopped), never "stuck".
  const ending: "failed" | "stopped" | null = act.status === "error" || failure ? "failed"
    : act.status === "stale" ? "stopped" : null;
  const stuck = !!ending;
  const reason = (failure || "").trim();
  const said = detail?.turns ?? [];
  return (
    <section className="ag-signed" data-testid="ag-signed-by" aria-label="Signed by">
      <span className="area-name ag-signed-title">Signed by</span>
      {stuck ? (
        <span className="ag-stuck" data-testid="ag-stuck" data-state={ending ?? undefined}
              data-tip={reason || (ending === "stopped" ? "Left open when the run stopped" : "Stopped with an error")}>
          <RunFace roleKey={who.roleKey} variant={who.variant} size={32} state={ending ?? "failed"} />
          <span className="ag-stuck-line">
            {ending === "stopped" ? `${named.name}: stopped${reason ? `, ${reason}` : " when the run stopped"}`
              : `${named.name}: failed, ${reason || "an error"}`}
          </span>
        </span>
      ) : null}
      <ActCard act={act} viewer={viewer} inline askless={askless} />
      {approval ? (
        <div className="ag-signed-approval" data-testid="ag-signed-approval">
          <ActCard act={approval} viewer={viewer} inline />
        </div>
      ) : null}
      {said.length ? (
        <div className="ag-said" data-testid="ag-said">
          <span className="area-name">Said</span>
          {said.map((t, i) => (
            <blockquote key={i} className="ag-said-turn" data-user-text>
              <RunFace roleKey={who.roleKey} variant={who.variant} size={20} />
              <span>{trimSaid(t.text)}</span>
            </blockquote>
          ))}
        </div>
      ) : null}
      {who.roleKey && !askless ? (
        <button type="button" className="cap-btn ag-signed-ask" data-testid="ag-signed-ask"
                data-tip={`Talk with ${named.name} about this step`}
                onClick={() => askAboutAct(act, question)}>
          Ask {named.name}
        </button>
      ) : null}
    </section>
  );
}
