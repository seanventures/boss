"use client";

/**
 * BUILD SPEED, how many parallel sub-agents a build's writing stages may use (engine
 * project_settings.build_parallelism). Balanced (the default) asks for 3, Fastest for 6; the
 * machine caps either at half its cores and what free memory carries. Speed is bought with more
 * tokens at once: the model and every check stay the same. Owner/Admin (settings:update).
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { fetchSetup, setupWrite } from "../../lib/setup-client";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";

const TIP = "Parallel sub-agents write a build's tests and code at once. More of them is faster and "
  + "uses more tokens at once; same model, same checks.";
const MODES = [
  { key: "balanced", label: "Balanced", tip: "Up to 3 sub-agents at once (the default)." },
  { key: "fastest", label: "Fastest", tip: "Up to 6 at once: more tokens per minute, same checks." },
] as const;

export function BuildSpeedRow() {
  const { projectId, view, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  const speed = view.build_speed;
  if (!speed) return null;
  const mode = speed.mode === "fastest" ? "fastest" : "balanced";
  const choose = async (next: string) => {
    if (next === mode) return;
    setBusy(true);
    try {
      await setupWrite(projectId, "build_speed", { key: speed.key, value: next },
                       { label: `Build speed: ${next}` });
      apply(await fetchSetup(projectId));
      toast(`Build speed: ${MODES.find((m) => m.key === next)?.label ?? next}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Could not change it", "error");
    } finally { setBusy(false); }
  };
  const asked = speed.asks?.[mode] ?? (mode === "fastest" ? 6 : 3);
  const capped = speed.parallelism < asked;
  return (
    <section className="su-row" data-testid="su-row-build-speed" aria-label="Build speed" id="su-build-speed">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Build speed</SectionLabel></div>
      <div className="su-row-body su-stack">
        <div className="su-line" role="radiogroup" aria-label="Build speed">
          {MODES.map((m) => (
            <GatedButton key={m.key} permission="settings:update" type="button" role="radio"
                         className={`su-btn su-btn-sm${m.key === mode ? " su-btn-primary" : ""}`}
                         aria-checked={m.key === mode} disabled={busy} onClick={() => void choose(m.key)}
                         data-testid={`su-build-speed-${m.key}`} title={m.tip} data-tip={m.tip}>
              {m.label}
            </GatedButton>
          ))}
          <span className="su-faint" data-testid="su-build-speed-now">
            {speed.parallelism} sub-agent{speed.parallelism === 1 ? "" : "s"} at once
            {capped ? `, this machine allows ${speed.machine}` : ""}
          </span>
        </div>
      </div>
    </section>
  );
}
