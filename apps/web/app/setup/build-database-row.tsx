"use client";

/**
 * GIVE BUILDS A DATABASE (Postgres) — the project setting `build_services`
 * (engine/boss_engine/services/build_services.py). It used to be reachable only from an MCP tool or
 * a repo's boss.verify.yaml; a first-run operator whose app needs a database never found it.
 * On: every build of this project gets its own empty PostgreSQL database (DATABASE_URL in the
 * build), dropped when the build ends. Owner/Admin (settings:update, with step-up).
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { fetchSetup, setupWrite } from "../../lib/setup-client";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";
import { Dot, Tip } from "./pill";

const TIP = "Each build gets its own empty PostgreSQL database (DATABASE_URL), dropped when the build ends. "
  + "Off leaves it to the repo's boss.verify.yaml (services: [postgres]).";

export function BuildDatabaseRow() {
  const { projectId, view, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  const db = view.build_database;
  if (!db) return null;
  const flip = async () => {
    setBusy(true);
    try {
      await setupWrite(projectId, "build_database", { key: db.key, value: db.enabled ? "off" : "postgres" },
                       { label: db.enabled ? "Stop giving builds a database" : "Give builds a database" });
      apply(await fetchSetup(projectId));
      toast(db.enabled ? "Builds no longer get a database" : "Each build now gets its own Postgres");
    } catch (error) {
      toast(error instanceof Error ? error.message : "Could not change it", "error");
    } finally { setBusy(false); }
  };
  return (
    <section className="su-row" data-testid="su-row-build-database" aria-label="Build database" id="su-build-database">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Build database</SectionLabel></div>
      <div className="su-row-body su-stack">
        <div className="su-line">
          <Tip tip={TIP} className="su-faint">Give builds a database (Postgres)</Tip>
          <GatedButton permission="settings:update" type="button" className="su-btn su-btn-sm"
                       aria-pressed={db.enabled} disabled={busy} onClick={flip} data-testid="su-build-database-toggle"
                       title={TIP} data-tip={TIP}>
            {db.enabled ? "On" : "Off"}
          </GatedButton>
        </div>
        {db.enabled && !db.configured ? (
          <span className="su-line" data-testid="su-build-database-unconfigured">
            <Dot tone="amber" />This server has no build database service (BOSS_BUILD_SERVICES_PG_URL is not set),
            so builds that need one stop and say so.
          </span>
        ) : null}
      </div>
    </section>
  );
}
