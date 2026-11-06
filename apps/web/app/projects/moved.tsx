"use client";

/**
 * "Moved to Project › Settings" (contract §11): the one line an old home of a project setting shows
 * now, linking to where it lives. The old control stays beside it until every install's engine
 * serves Project › Settings, so nothing is lost on the way (README parity table).
 */
import React from "react";
import { ArrowRight, GearSix } from "@phosphor-icons/react";

import { consoleHref } from "../console-href";

export function MovedToProjectSettings({ section, testid = "moved-to-project-settings" }: { section: string; testid?: string }) {
  return (
    <a className="px-moved" href={consoleHref({ area: "project", tab: "settings", section, app: undefined, build: undefined })}
       data-testid={testid} title="This setting lives on the project's page now"
       style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "0.8rem", color: "var(--accent-text)", textDecoration: "none" }}>
      <GearSix size={13} aria-hidden /> Moved to Project › Settings <ArrowRight size={11} aria-hidden />
    </a>
  );
}
