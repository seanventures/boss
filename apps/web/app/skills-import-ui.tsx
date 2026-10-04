"use client";

import React, { useState, useTransition } from "react";
import { useConsoleRefresh } from "./use-console-refresh";

import { exportSkillAction, importSkillAction } from "./actions";
import { SubmitButton } from "./submit-button";

import { callAction } from "../lib/call-action";
export function SkillImportForm({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const refresh = useConsoleRefresh();
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  return (
    <form
      className="composer"
      data-testid="skill-import"
      action={(formData) => {
        setError(null);
        startTransition(async () => {
          try {
            // A result, not a throw: see lib/action-result.ts.
            const result = await callAction(importSkillAction)(formData);
            if (!result.ok) {
              setError(result.error ?? "Skill import failed.");
              return;
            }
            refresh("now");
          } catch (err) {
            setError(err instanceof Error ? err.message : "Skill import failed.");
          }
        });
      }}
    >
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <h4>Import skill YAML</h4>
      <p className="detail">
        Local Community import. Requires pack_manage. Hosted marketplace stays
        hidden on Community — paste or upload a skill capability-bundle YAML here.
        Unsigned local bundles use the explicit untrusted override.
      </p>
      <label className="field">
        <span>Skill YAML</span>
        <textarea
          className="input"
          name="yaml"
          rows={8}
          spellCheck={false}
          placeholder={'format: boss.capability-bundle/v1\nkind: skill\nid: my.local.skill'}
        />
      </label>
      <label className="field">
        <span>Or upload a .yaml file</span>
        <input
          className="input"
          type="file"
          name="yaml_file"
          accept=".yaml,.yml,.json,text/yaml,application/x-yaml,application/json,application/vnd.boss.capability+json"
        />
      </label>
      <SubmitButton className="button secondary" type="submit">
        Import skill YAML
      </SubmitButton>
      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}

export function SkillExportButton({
  projectId,
  skillId,
  actor,
}: {
  projectId: string;
  skillId: string;
  actor: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  return (
    <form
      data-testid="skill-export"
      action={(formData) => {
        setError(null);
        startTransition(async () => {
          try {
            const result = await callAction(exportSkillAction)(formData);
            if (!result.ok || !result.data) {
              setError(result.error ?? "Skill export failed.");
              return;
            }
            downloadSkillYaml(String(result.data.skillId), String(result.data.yaml));
          } catch (err) {
            setError(err instanceof Error ? err.message : "Skill export failed.");
          }
        });
      }}
    >
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <input type="hidden" name="skill_id" value={skillId} />
      <SubmitButton className="button secondary" type="submit">
        Export
      </SubmitButton>
      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}

function downloadSkillYaml(skillId: string, yaml: string) {
  const blob = new Blob([yaml], { type: "application/x-yaml" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${skillId}.yaml`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
