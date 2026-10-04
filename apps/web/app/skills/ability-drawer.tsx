"use client";

/**
 * The ability drawer (spec §5.3): right, 420px; a bottom sheet under 720px. Header (icon, label,
 * the one status, provenance, ⋯), then five sections with 11px caps labels and an (i) tooltip —
 * Connection, Tools, Agents, Used in, Try it. No paragraphs: every write is immediate and toasted.
 */
import React, { useEffect, useRef, useState } from "react";
import { DotsThree, X } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi, type CrewMember } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { AbilityIcon } from "./ability-icons";
import { StatusPill } from "./ability-card";
import { AgentsSection } from "./agents-section";
import { ConnectionSection } from "./connection-section";
import { PROVENANCE, shortError, TIP } from "./kit-copy";
import { DrawerShell } from "./overlay";
import { ToolsSection } from "./tools-section";
import { TrySection } from "./try-section";
import { UsedInSection } from "./used-in-section";

export type DrawerFocus = "connection" | "tools" | "agents" | null;

export function Section({ id, label, tip, children, focus }: {
  id: string; label: string; tip: string; children: React.ReactNode; focus?: boolean;
}) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => { if (focus) ref.current?.scrollIntoView?.({ block: "start", behavior: "smooth" }); }, [focus]);
  return (
    <section ref={ref} className="kit-section" data-testid={`kit-section-${id}`} data-focus={focus || undefined}>
      <h5 className="kit-section-label">
        <span>{label}</span>
        <span className="kit-info" data-tip={tip} aria-label={tip} role="img">i</span>
      </h5>
      {children}
    </section>
  );
}

function exportAbility(ability: Ability) {
  const data = { key: ability.key, label: ability.label, tools: ability.tools.map((t) => ({ ref: t.ref, policy: t.policy })),
                 holders: ability.holders, provider: ability.connection?.provider ?? null };
  try {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = `${ability.key}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch { /* nothing to download into */ }
}

export function AbilityDrawer({ ability, crew, projectId, actor, focus, testing, onClose, onChanged, onReload }: {
  ability: Ability;
  crew: CrewMember[];
  projectId: string;
  actor: string;
  focus: DrawerFocus;
  testing: boolean;
  onClose: () => void;
  onChanged: (a: Ability) => void;
  onReload: () => void;
}) {
  const [menu, setMenu] = useState(false);
  const provenance = PROVENANCE[ability.provenance] ?? PROVENANCE.built_in;
  const toggle = async (enabled: boolean) => {
    setMenu(false);
    const r = await kitApi.enabled(projectId, actor, ability.key, enabled);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", enabled ? `${ability.label} on` : `${ability.label} off`);
    onChanged(r.data);
  };

  return (
    <DrawerShell label={ability.label} testId={`kit-drawer-${ability.key}`} onClose={onClose}>
      <header className="kit-drawer-head">
        <AbilityIcon name={ability.icon} />
        <h3 className="kit-drawer-title" data-tip={ability.tip}>{ability.label}</h3>
        <StatusPill status={testing ? "testing" : ability.status} tip={ability.status_tip} />
        <span className="kit-chip" data-tip={provenance.tip}>{provenance.label}</span>
        <span className="kit-spacer" />
        <span className="kit-confirm-wrap">
          <button type="button" className="kit-icon-btn" aria-label="More" data-testid="kit-drawer-more"
                  onClick={() => setMenu((m) => !m)}><DotsThree size={18} /></button>
          {menu ? (
            <span className="kit-pop kit-menu" role="menu" data-testid="kit-drawer-menu">
              {ability.status === "off"
                ? <button type="button" role="menuitem" onClick={() => void toggle(true)}>Turn on</button>
                : <button type="button" role="menuitem" data-testid="kit-drawer-off" onClick={() => void toggle(false)}>Turn off</button>}
              <button type="button" role="menuitem" onClick={() => { setMenu(false); exportAbility(ability); }}>Export</button>
            </span>
          ) : null}
        </span>
        <button type="button" className="kit-icon-btn" aria-label="Close" data-testid="kit-drawer-close" onClick={onClose}>
          <X size={16} />
        </button>
      </header>
      {ability.status_line && (ability.status === "failing" || ability.status === "review") ? (
        <div className="kit-card-line" data-tone={ability.status === "failing" ? "red" : "amber"}>
          <span data-tip={ability.status_tip}>{ability.status_line}</span>
        </div>
      ) : null}
      <div className="kit-drawer-body">
        <Section id="connection" label="Connection" tip="The account or key this ability uses" focus={focus === "connection"}>
          <ConnectionSection ability={ability} projectId={projectId} actor={actor} onChanged={onChanged} />
        </Section>
        <Section id="tools" label="Tools" tip="What agents may call, and when they ask" focus={focus === "tools"}>
          <ToolsSection ability={ability} projectId={projectId} actor={actor}
                        onChanged={(a) => (a ? onChanged(a) : onReload())} />
        </Section>
        <Section id="agents" label="Agents" tip="Who may use this ability" focus={focus === "agents"}>
          <AgentsSection ability={ability} crew={crew} projectId={projectId} actor={actor} onChanged={onChanged} />
        </Section>
        <Section id="used" label="Used in" tip={TIP.usedIn}>
          <UsedInSection ability={ability} />
        </Section>
        {ability.try ? (
          <Section id="try" label="Try it" tip={TIP.tryIt}>
            <TrySection ability={ability} projectId={projectId} actor={actor} />
          </Section>
        ) : null}
      </div>
    </DrawerShell>
  );
}
