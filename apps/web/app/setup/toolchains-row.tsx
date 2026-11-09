"use client";

/**
 * BUILD TOOLCHAINS: what the build sandbox can build and run on this install. Python and Node
 * are always there; Java (and later stacks) is turned on per install with BOSS_TOOLCHAINS, so
 * a stack that is off says how to turn it on. Read from the Setup view (`toolchains`).
 */
import React from "react";

import type { Toolchain } from "../../lib/setup-types";
import { useBoard } from "./parts";
import { SectionLabel } from "../section-label";

function tipFor(t: Toolchain): string {
  if (!t.installed) return `Off. Turn it on on the BOSS host: ${t.enable ?? `boss toolchains enable ${t.name}`}`;
  const versions = Object.entries(t.versions).map(([k, v]) => `${k} ${v}`).join(", ");
  return versions || "Always installed.";
}

export function ToolchainsRow() {
  const list = useBoard().view.toolchains;
  if (!list?.length) return null;
  return (
    <section className="su-row" data-testid="su-row-toolchains" aria-label="Toolchains" id="su-toolchains">
      <div className="su-row-label">
        <SectionLabel as="span" tip="What builds can compile, test and run on this install.">Toolchains</SectionLabel>
      </div>
      <div className="su-row-body su-line">
        {list.map((t) => {
          const tone = t.installed ? "green" : "busy";
          const tip = tipFor(t);
          return (
            <span key={t.name} className={`su-pill su-tone-${tone}`} data-testid={`toolchain-${t.name}`}
                  data-installed={t.installed || undefined} title={tip} data-tip={tip}>
              {t.installed ? <span className="su-dot su-tone-green" aria-hidden="true" /> : null}
              {t.label.split(" (")[0]}{t.installed ? "" : ", off"}
            </span>
          );
        })}
      </div>
    </section>
  );
}
