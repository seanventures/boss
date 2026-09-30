"use client";

/**
 * The project's default team, chosen where the workflow is chosen.
 *
 * "I did not see option in the command center to assign the agents at the time of creation —
 *  this design is very confusing." Fair, and the reason is structural rather than an oversight:
 * the Command Center's Build does not create a project. It sends the directive down panel → PRD
 * → code, so at the moment of pressing it there is no app for anyone to be assigned to.
 *
 * What can be chosen here is exactly what the workflow picker beside it chooses — a DEFAULT for
 * this project. It seeds every app that follows, and each app can then differ on its own
 * workflow. One control per level: the project's team here, this app's team there.
 *
 * Same endpoint and same table as the per-app assignment, filed against `app_key = "*"`, so
 * there is one way a team gets set rather than two that can disagree.
 */

import { useCallback, useEffect, useState } from "react";

import { AgentChip } from "./agent-chip";
import { notifyChanged } from "./changed";

const PROJECT_DEFAULT = "*";

export function DefaultAgentPicker({ projectId, actor }: {
  projectId: string; actor: string;
}) {
  const [team, setTeam] = useState<string[] | null>(null);
  const [roles, setRoles] = useState<Array<{ role_key: string; display_name?: string }>>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const [t, r] = await Promise.all([
        fetch(`/api/project-agents?project_id=${encodeURIComponent(projectId)}`
              + `&app=${encodeURIComponent(PROJECT_DEFAULT)}`, { cache: "no-store" }),
        fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
              { cache: "no-store" }),
      ]);
      if (t.ok) {
        const body = await t.json();
        setTeam(((body?.agents ?? []) as Array<{ role_key: string }>).map((a) => a.role_key));
      }
      if (r.ok) setRoles(((await r.json())?.items ?? []).map((x: Record<string, unknown>) => ({
        role_key: String(x.role_key ?? ""), display_name: String(x.display_name ?? ""),
      })));
    } catch { /* the picker is unavailable; BOSS still uses its built-in team */ }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  const act = useCallback(async (body: Record<string, unknown>) => {
    setBusy(true);
    try {
      const r = await fetch("/api/project-agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, app: PROJECT_DEFAULT, actor, ...body }),
      });
      const data = await r.json().catch(() => ({}));
      setNote(r.ok ? "Saved" : String(data?.error ?? `failed (${r.status})`));
      if (r.ok) { await load(); notifyChanged("agents"); }
    } catch (e) {
      setNote(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  }, [projectId, actor, load]);

  const nameOf = (key: string) =>
    roles.find((r) => r.role_key === key)?.display_name || key.replace(/_/g, " ");
  const current = team ?? [];
  const available = roles.filter((r) => !current.includes(r.role_key));

  return (
    <div className="askboss-wf askboss-agents" data-testid="askboss-default-agents">
      <button type="button" className="askboss-wf-label askboss-agents-toggle"
              onClick={() => setOpen((o) => !o)}
              title="The agents every new project here starts with">
        Agents {current.length ? `· ${current.length}` : ""} {open ? "▾" : "▸"}
      </button>
      {note ? <span className="askboss-wf-note">{note}</span> : null}

      {open ? (
        <div className="askboss-agents-panel">
          <p className="detail">
            {/* NOT "nobody". An empty list here means BOSS's own set runs, which is a different
                statement and the one that is true. */}
            {current.length
              ? "Every new project here starts with these. Each one can differ on its workflow."
              : "No team chosen, so BOSS uses its built-in set — the agents that run research, "
                + "the panel and the build anyway. Pick some to make it a decision."}
          </p>
          <div className="aot-list">
            {current.map((key) => (
              <AgentChip key={key} roleKey={key} label={nameOf(key)}
                         href={`?area=workforce&role=${encodeURIComponent(key)}`}>
                <button type="button" className="aot-x" disabled={busy}
                        aria-label={`Remove ${nameOf(key)}`}
                        onClick={() => void act({ action: "unassign", role_key: key })}>✕</button>
              </AgentChip>
            ))}
            <select className="mr-select aot-pick" value="" disabled={busy}
                    data-testid="askboss-agent-add"
                    onChange={(e) => {
                      if (e.target.value) void act({ role_keys: [e.target.value] });
                    }}>
              <option value="">+ Add an agent…</option>
              {available.map((r) => (
                <option key={r.role_key} value={r.role_key}>
                  {r.display_name || r.role_key}
                </option>
              ))}
            </select>
          </div>
        </div>
      ) : null}
    </div>
  );
}
