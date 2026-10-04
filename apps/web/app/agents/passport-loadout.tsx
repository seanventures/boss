"use client";

/**
 * LOADOUT, the Passport's first section (agent-loadout spec §3): what the agent can do and how
 * it is told to work — `Skills`, `MCP` and `Instructions` — above everything it has done.
 *
 * The Passport used to be ordered by history: skills and MCP were its 9th to 12th blocks, under
 * the apps, panels and assignments, and on three surfaces at once. An operator asked for them
 * "defined at the beginning", with a way to upload a file of instructions or a skill. This is
 * that: one row per kind of grant, each held thing once, and the instructions with their budget.
 *
 * `⋯ › See its prompt` shows the composed prompt by layer, so an upload can be seen to land.
 */
import React, { useRef, useState } from "react";
import { DotsThree } from "@phosphor-icons/react";

import { readTextFile } from "../../lib/agent-instructions";
import { showToast } from "../client-forms";
import { SectionLabel } from "../section-label";
import {
  LoadoutGrants, type AvailableServer, type HeldServer, type HeldSkill,
} from "../skills/agent-abilities";
import { InstructionsRow } from "./instructions-row";
import { Pop } from "./loadout-pop";
import { PromptDrawer } from "./prompt-drawer";
import { SkillFileSheet } from "./skill-file-sheet";

export const LOADOUT_TIP = "What it can do and how it's told to work";
export const PROMPT_TIP = "Exactly what the model is given, without the task";

export function PassportLoadout({ projectId, roleKey, actor, agentName, registered, skills, mcp, allowed, forbidden,
                                  onRegister, onChanged, onVersion }: {
  projectId: string; roleKey: string; actor: string; agentName: string; registered: boolean;
  skills?: { holds: HeldSkill[]; available: HeldSkill[] };
  mcp?: { holds: HeldServer[]; available: AvailableServer[] };
  allowed: string[]; forbidden: string[];
  onRegister: () => void;
  /** A grant changed: re-read the agent (its skills, servers and version). */
  onChanged: () => void;
  /** A new version from an instruction change, for the certificate. */
  onVersion: (version: number) => void;
}) {
  const [canEdit, setCanEdit] = useState(false);
  const [skillFile, setSkillFile] = useState<{ fileName: string; text: string } | null>(null);
  const [prompt, setPrompt] = useState(false);
  const [reload, setReload] = useState(0);
  const filePick = useRef<HTMLInputElement>(null);

  const onSkillFile = async (file: File | undefined) => {
    if (!file) return;
    const read = await readTextFile(file);
    if (filePick.current) filePick.current.value = "";
    if (!read.ok) { showToast("error", read.error); return; }
    setSkillFile({ fileName: read.name, text: read.text });
  };

  return (
    <section className="ct-block ld" id="ag-sec-loadout" data-testid="passport-loadout">
      {/* The Kit's links still say `&tab=skills`: the old anchor lands here too. */}
      <span id="ag-sec-skills" className="ld-anchor" aria-hidden="true" />
      <div className="ld-head">
        <SectionLabel level={1} as="h2" tip={LOADOUT_TIP} testid="loadout-title">Loadout</SectionLabel>
        {registered && canEdit ? (
          <Pop label="Loadout options" testId="loadout-more" className="ld-icon" align="right" trigger={<DotsThree size={16} weight="bold" />}>
            {(close) => (
              <button type="button" role="menuitem" data-tip={PROMPT_TIP} data-testid="loadout-see-prompt"
                      onClick={() => { close(); setPrompt(true); }}>See its prompt</button>
            )}
          </Pop>
        ) : null}
      </div>
      {!registered ? (
        <div className="ag-unreg" data-testid="agent-unregistered"
             title="Ungoverned: no registered role, so no limits or trust score">
          <strong>Not registered</strong>
          <button type="button" className="ag-btn ag-btn-primary" data-testid="agent-register"
                  title="Make it an agent: skills, limits and a trust score" onClick={onRegister}>Register</button>
        </div>
      ) : (
        <>
          <LoadoutGrants projectId={projectId} roleKey={roleKey} actor={actor} canEdit={canEdit}
                         skills={skills?.holds ?? []} available={skills?.available ?? []}
                         servers={mcp?.holds ?? []} availableServers={mcp?.available ?? []}
                         allowed={allowed} forbidden={forbidden} onChanged={onChanged}
                         onFromFile={() => filePick.current?.click()} />
          <InstructionsRow projectId={projectId} roleKey={roleKey} actor={actor} reloadKey={reload}
                           onAccess={setCanEdit} onVersion={onVersion}
                           onMakeSkill={(text, fileName) => setSkillFile({ text, fileName })} />
          <input ref={filePick} type="file" accept=".md,.markdown,.txt,text/markdown" hidden
                 data-testid="loadout-skill-file" onChange={(e) => void onSkillFile(e.target.files?.[0])} />
        </>
      )}
      {skillFile ? (
        <SkillFileSheet projectId={projectId} actor={actor} roleKey={roleKey} agentName={agentName}
                        fileName={skillFile.fileName} text={skillFile.text} onClose={() => setSkillFile(null)}
                        onCreated={(label, version) => {
                          setSkillFile(null);
                          showToast("success", `Created ${label} · gave it to ${agentName}`);
                          if (version) onVersion(version);
                          setReload((n) => n + 1);
                          onChanged();
                        }} />
      ) : null}
      {prompt ? <PromptDrawer projectId={projectId} actor={actor} roleKey={roleKey} onClose={() => setPrompt(false)} /> : null}
    </section>
  );
}
