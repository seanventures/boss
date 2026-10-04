"use client";

import React, { useState, useTransition } from "react";
import { useConsoleRefresh } from "./use-console-refresh";

import { exportPackAction, importPackAction } from "./actions";
import { SubmitButton } from "./submit-button";

import { callAction } from "../lib/call-action";
export function PackImportForm({
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
      data-testid="pack-import"
      action={(formData) => {
        setError(null);
        startTransition(async () => {
          try {
            // A result, not a throw: see lib/action-result.ts.
            const result = await callAction(importPackAction)(formData);
            if (!result.ok) {
              setError(result.error ?? "Pack import failed.");
              return;
            }
            refresh("now");
          } catch (err) {
            setError(err instanceof Error ? err.message : "Pack import failed.");
          }
        });
      }}
    >
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <h4>Import pack YAML</h4>
      <p className="detail">
        Local Community import. Requires pack_manage. Hosted marketplace stays
        hidden on Community — paste or upload a pack YAML here.
      </p>
      <label className="field">
        <span>Pack YAML</span>
        <textarea
          className="input"
          name="yaml"
          rows={8}
          spellCheck={false}
          placeholder={"id: my_pack_v1\nname: My pack\nversion: 0.1.0"}
        />
      </label>
      <label className="field">
        <span>Or upload a .yaml file</span>
        <input
          className="input"
          type="file"
          name="yaml_file"
          accept=".yaml,.yml,text/yaml,application/x-yaml"
        />
      </label>
      <SubmitButton className="button secondary" type="submit">
        Import pack YAML
      </SubmitButton>
      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}

export function PackExportButton({
  projectId,
  packId,
  actor,
}: {
  projectId: string;
  packId: string;
  actor: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  return (
    <form
      data-testid="pack-export"
      action={(formData) => {
        setError(null);
        startTransition(async () => {
          try {
            const result = await callAction(exportPackAction)(formData);
            if (!result.ok || !result.data) {
              setError(result.error ?? "Pack export failed.");
              return;
            }
            downloadPackYaml(String(result.data.packId), String(result.data.yaml));
          } catch (err) {
            setError(err instanceof Error ? err.message : "Pack export failed.");
          }
        });
      }}
    >
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <input type="hidden" name="pack_id" value={packId} />
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

function downloadPackYaml(packId: string, yaml: string) {
  const blob = new Blob([yaml], { type: "application/x-yaml" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${packId}.yaml`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
