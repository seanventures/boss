"use client";

/**
 * BUILD DATABASE — an OVERRIDE, not a step (engine services/build_services.py `decide`). By
 * default (Auto) a build gets its own empty PostgreSQL database when BOSS detects the app needs
 * one: the brief or PRD names PostgreSQL, a template needs it, the plan records
 * `database: postgres`, or the app's code declares it. Always gives every build one; Never none.
 * Owner/Admin (settings:update). The build page says "Database: Postgres for this build" and why.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { fetchSetup, setupWrite } from "../../lib/setup-client";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";
import { Dot } from "./pill";

const TIP = "Auto gives a build its own empty PostgreSQL (DATABASE_URL), dropped when it ends, whenever "
  + "the brief, a template, the plan or the app's code says PostgreSQL. Always: every build. Never: none.";
const MODES = [
  { key: "auto", label: "Auto", tip: "When BOSS detects the app needs one (the default)." },
  { key: "always", label: "Always", tip: "Every build of this project gets one." },
  { key: "never", label: "Never", tip: "No build gets one, even when the repo asks." },
] as const;

export function BuildDatabaseRow() {
  const { projectId, view, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  const db = view.build_database;
  if (!db) return null;
  const mode = db.mode ?? (db.enabled ? "always" : "auto");
  const choose = async (next: string) => {
    if (next === mode) return;
    setBusy(true);
    try {
      await setupWrite(projectId, "build_database", { key: db.key, value: next },
                       { label: `Build database: ${next}` });
      apply(await fetchSetup(projectId));
      toast(`Build database: ${MODES.find((m) => m.key === next)?.label ?? next}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Could not change it", "error");
    } finally { setBusy(false); }
  };
  return (
    <section className="su-row" data-testid="su-row-build-database" aria-label="Build database" id="su-build-database">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Build database</SectionLabel></div>
      <div className="su-row-body su-stack">
        <div className="su-line" role="radiogroup" aria-label="Build database">
          {MODES.map((m) => (
            <GatedButton key={m.key} permission="settings:update" type="button" role="radio"
                         className={`su-btn su-btn-sm${m.key === mode ? " su-btn-primary" : ""}`}
                         aria-checked={m.key === mode} disabled={busy} onClick={() => void choose(m.key)}
                         data-testid={`su-build-database-${m.key}`} title={m.tip} data-tip={m.tip}>
              {m.label}
            </GatedButton>
          ))}
          <span className="su-faint">{mode === "auto" ? "Postgres per build when the app needs one" : mode === "always" ? "Postgres for every build" : "No build database"}</span>
        </div>
        {mode !== "never" && !db.configured ? (
          <span className="su-line" data-testid="su-build-database-unconfigured">
            <Dot tone="amber" />This server has no build database service (BOSS_BUILD_SERVICES_PG_URL is not set),
            so a build that needs one stops and says so.
          </span>
        ) : null}
      </div>
    </section>
  );
}
