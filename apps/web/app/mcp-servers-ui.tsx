"use client";

/**
 * MCP servers, managed in one component wherever they are shown.
 *
 * WHY ONE COMPONENT. The same servers were listed on three screens with three stories:
 * Skills and MCP said registration was "deliberately not a form here", System Configuration
 * said "Register one via POST /v1/mcp/servers", and the only working form sat on Capabilities,
 * which the Community rail does not show. None of them could edit a server, none said a server
 * was unreachable (the three BOSS seeds point at 127.0.0.1 ports the community compose never
 * starts, and every screen called them enabled), and Remove deleted a server out from under
 * the steps bound to it with no question asked. Every screen now mounts this, so they cannot
 * disagree again: `manage` where servers are changed, `summary` where they are only surveyed.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";

import { apiWrite } from "../lib/api-write";

export type McpTool = { name: string; description?: string; inputSchema?: unknown };

export type McpServerRow = {
  id: string; name: string; url: string; transport?: string; risk_tier: number;
  data_boundary?: string; enabled: boolean; description?: string | null;
  trusted?: boolean; reachable?: boolean | null; last_checked_at?: string | null;
  last_error?: string | null; status_note?: string | null; tools?: McpTool[];
  tool_risk_overrides?: Record<string, number>;
  provenance?: { reviewed_by?: string; source?: string };
  references?: {
    custom_steps: Array<{ step_key: string; label?: string; tool?: string }>;
    skills: Array<{ skill_key: string; label?: string; tool?: string }>;
    // Agents that HOLD the server (role_mcp_servers). Not a blocker like the two above: the
    // operator may take it off them in the same press, after being told who they are.
    agents?: Array<{ role_key: string; tools?: string[] }>;
  };
};

const BOUNDARIES = ["internal", "public", "confidential", "restricted", "local-only"];

/** The four tiers, said as consequences. The Capabilities form had no tier 0 at all and called
 *  tier 1 "auto", which is a mode, not a risk. */
export const MCP_TIERS = [
  { value: 0, label: "0 — reads only" },
  { value: 1, label: "1 — writes something recoverable" },
  { value: 2, label: "2 — changes the world (always needs a person)" },
  { value: 3, label: "3 — irreversible and public" },
];

const BLANK = { name: "", url: "", description: "", risk_tier: "1",
                data_boundary: "internal", trusted: false, overrides: "" };

/** "tool = tier" per line → {tool: tier}. Returns an error sentence instead when a line does
 *  not parse, so the operator is told which line rather than getting a 422 about a dict. */
export function parseOverrides(raw: string): Record<string, number> | string {
  const out: Record<string, number> = {};
  for (const line of raw.split(/\n/).map((l) => l.trim()).filter(Boolean)) {
    const m = /^([^=:\s]+)\s*[=:]\s*([0-3])$/.exec(line);
    if (!m) return `"${line}" is not "tool = tier" with a tier from 0 to 3.`;
    out[m[1]] = Number(m[2]);
  }
  return out;
}

function overridesText(o?: Record<string, number>): string {
  return Object.entries(o ?? {}).map(([k, v]) => `${k} = ${v}`).join("\n");
}

/** Reachability as a sentence-sized badge. `null` is "never checked", which is NOT "down". */
function reach(s: McpServerRow): { label: string; tone: string } {
  if (!s.enabled) return { label: "off", tone: "muted" };
  if (s.reachable === true) return { label: "reachable", tone: "green" };
  if (s.reachable === false) return { label: "unreachable", tone: "red" };
  return { label: "not checked yet", tone: "muted" };
}

export function McpServersPanel({
  projectId, actor, mode = "manage", initial, onLoaded,
}: {
  projectId: string;
  actor: string;
  /** `manage` edits; `summary` lists with status and links to the screen that edits. */
  mode?: "manage" | "summary";
  /** What the server render already has, so the first paint is not "Loading…". */
  initial?: McpServerRow[];
  /** Told the list whenever it is read, so a sibling (the skill form's server picker) uses
   *  the same rows instead of fetching its own. */
  onLoaded?: (rows: McpServerRow[]) => void;
}) {
  const [servers, setServers] = useState<McpServerRow[] | null>(initial ?? null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editedUrl, setEditedUrl] = useState("");
  const [form, setForm] = useState({ ...BLANK });
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const load = useCallback(async (check: "none" | "stale" | "now") => {
    try {
      const r = await fetch(`/api/skills?view=mcp&check=${check}`
        + `&project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        setNote({ ok: false, text: `Could not list MCP servers: ${String(data?.error ?? r.status)}` });
        setServers((prev) => prev ?? []);
        return;
      }
      const rows = (data.items ?? []) as McpServerRow[];
      setServers(rows);
      onLoaded?.(rows);
    } catch (e) {
      setNote({ ok: false, text: `Could not list MCP servers: ${String((e as Error)?.message ?? e)}` });
      setServers((prev) => prev ?? []);
    }
  }, [projectId, onLoaded]);

  // Read what was recorded first (instant), then ask the servers not checked in the last
  // minute — so the list appears at once and the reachability fills in after.
  useEffect(() => { void (async () => { await load("none"); await load("stale"); })(); }, [load]);

  const send = useCallback(async (payload: Record<string, unknown>, done: string) => {
    if (busy) return false;
    setBusy(true); setNote(null);
    try {
      const r = await apiWrite("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, ...payload }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) {
        // The API's sentences say what happened and what to do; shown as they are.
        setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) });
        return false;
      }
      setNote({ ok: true, text: done });
      void load("stale");
      return true;
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
      return false;
    }
  }, [busy, projectId, actor, load]);

  const startEdit = (s: McpServerRow) => {
    setEditing(s.id);
    setEditedUrl(s.url);
    setForm({ name: s.name, url: s.url, description: s.description ?? "",
              risk_tier: String(s.risk_tier), data_boundary: s.data_boundary ?? "internal",
              trusted: Boolean(s.trusted), overrides: overridesText(s.tool_risk_overrides) });
  };
  const startNew = () => { setEditing("new"); setForm({ ...BLANK }); };

  const submit = async () => {
    const overrides = parseOverrides(form.overrides);
    if (typeof overrides === "string") { setNote({ ok: false, text: overrides }); return; }
    if (!form.name.trim() || !form.url.trim()) {
      setNote({ ok: false, text: "A server needs a name and the URL of its endpoint." });
      return;
    }
    const fields = {
      name: form.name.trim(), url: form.url.trim(), description: form.description.trim(),
      // Number(), not `Number(x) || 1`: tier 0 is falsy, and `|| 1` made it unsaveable.
      risk_tier: Number(form.risk_tier), data_boundary: form.data_boundary,
      tool_risk_overrides: overrides,
    };
    const ok = editing === "new"
      ? await send({ action: "mcp_register",
                     server: { ...fields, provenance: { trusted: form.trusted } } },
                   `Registered ${fields.name}. Checking whether BOSS can reach it…`)
      : await send({ action: "mcp_update", server_id: editing,
                     changes: { ...fields, trusted: form.trusted } },
                   `Saved ${fields.name}.`);
    if (ok) { setEditing(null); setForm({ ...BLANK }); }
  };

  const ordered = useMemo(() => [...(servers ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
                          [servers]);

  if (mode === "summary") {
    return (
      <div data-testid="mcp-summary">
        {servers === null ? <p className="detail">Loading…</p>
         : ordered.length === 0 ? (
           <p className="detail" data-testid="mcp-empty">
             No MCP servers registered. Register one on the{" "}
             <a href="?area=skills">Skills and MCP</a> page.
           </p>
         ) : (
          <ul className="skill-list" data-testid="mcp-summary-list">
            {ordered.map((s) => {
              const r = reach(s);
              return (
                <li key={s.id} className="skill-row" data-enabled={s.enabled}>
                  <span className="skill-row-main">
                    <b>{s.name}</b>
                    <span className={`cap-badge tone-${r.tone}`}>{r.label}</span>
                    <span className="cap-badge tone-muted">risk {s.risk_tier}</span>
                    {s.trusted ? null : <span className="cap-badge tone-pink">unreviewed</span>}
                  </span>
                  <code className="detail">{s.url}</code>
                  {s.status_note ? <span className="detail">{s.status_note}</span> : null}
                </li>
              );
            })}
          </ul>
        )}
        <p className="detail">
          Register, edit, check or remove servers on the{" "}
          <a href="?area=skills" data-testid="mcp-manage-link">Skills and MCP</a> page.
        </p>
        {note && !note.ok ? <p className="setup-bad">{note.text}</p> : null}
      </div>
    );
  }

  return (
    <div data-testid="mcp-panel">
      <div className="skill-assign-actions">
        <button type="button" className="cap-btn primary" data-testid="mcp-register-open"
                disabled={busy} onClick={startNew}>Register an MCP server</button>
        <button type="button" className="linklike" data-testid="mcp-check-all" disabled={busy}
                onClick={() => { setNote(null); void load("now"); }}>Check all now</button>
      </div>

      {editing ? (
        <div className="setup-form" data-testid="mcp-form">
          <p className="detail">
            {editing === "new"
              ? "The URL must be one the BOSS API container can reach — 127.0.0.1 inside the "
                + "container is the container itself, so use the service name or host address."
              : "Editing keeps the server's id, so every step and skill bound to it keeps "
                + "working. Changing the URL forgets the last check until the next one."}
          </p>
          <label className="setup-field"><span>Name</span>
            <input type="text" value={form.name} data-testid="mcp-name" placeholder="web-intel"
                   onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
          </label>
          <label className="setup-field"><span>URL (JSON-RPC endpoint)</span>
            <input type="text" value={form.url} data-testid="mcp-url" spellCheck={false}
                   placeholder="http://web-intel:8770/mcp"
                   onChange={(e) => {
                     const url = e.target.value;
                     // A REVIEW IS OF AN ADDRESS. Re-pointing a reviewed server at another host
                     // kept it "reviewed"; the API now voids the review on a URL change, and the
                     // form says so rather than silently sending the old answer back.
                     const moved = editing !== "new" && url.trim() !== editedUrl.trim();
                     setForm((f) => ({ ...f, url, ...(moved ? { trusted: false } : {}) }));
                   }} />
          </label>
          <label className="setup-field"><span>What it provides</span>
            <input type="text" value={form.description} data-testid="mcp-description"
                   onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} />
          </label>
          <label className="setup-field"><span>Risk for its tools</span>
            <select value={form.risk_tier} data-testid="mcp-risk"
                    onChange={(e) => setForm((f) => ({ ...f, risk_tier: e.target.value }))}>
              {MCP_TIERS.map((t) => <option key={t.value} value={String(t.value)}>{t.label}</option>)}
            </select>
          </label>
          <label className="setup-field"><span>Tools with their own tier — one per line</span>
            <textarea rows={3} value={form.overrides} data-testid="mcp-overrides"
                      spellCheck={false} placeholder={"delete_issue = 3\nsearch = 0"}
                      onChange={(e) => setForm((f) => ({ ...f, overrides: e.target.value }))} />
          </label>
          <label className="setup-field"><span>Data boundary</span>
            <select value={form.data_boundary} data-testid="mcp-boundary"
                    onChange={(e) => setForm((f) => ({ ...f, data_boundary: e.target.value }))}>
              {BOUNDARIES.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
          <label className="setup-field"><span>Reviewed</span>
            <select value={form.trusted ? "yes" : "no"} data-testid="mcp-trusted"
                    onChange={(e) => setForm((f) => ({ ...f, trusted: e.target.value === "yes" }))}>
              <option value="no">Not yet reviewed</option>
              <option value="yes">A person has checked this server and vouches for it</option>
            </select>
          </label>
          {editing !== "new" && form.url.trim() !== editedUrl.trim() ? (
            <p className="detail" data-testid="mcp-url-voids-review">
              A new address is a new server as far as a review goes: saving clears Reviewed.
              Choose it again only once a person has checked what answers at the new address.
            </p>
          ) : null}
          {Number(form.risk_tier) >= 2 && !form.trusted ? (
            <p className="detail" data-testid="mcp-risk-note">
              Until it is marked reviewed, its tools at tier 2 or above cannot be placed in a
              workflow.
            </p>
          ) : null}
          <span className="skill-assign-actions">
            <button type="button" className="cap-btn primary" data-testid="mcp-save"
                    disabled={busy} onClick={() => void submit()}>
              {busy ? "Saving…" : editing === "new" ? "Register server" : "Save changes"}
            </button>
            <button type="button" className="linklike" onClick={() => setEditing(null)}>Cancel</button>
          </span>
        </div>
      ) : null}

      {note ? (
        <p className={note.ok ? "setup-ok" : "setup-bad"} data-testid="mcp-note">{note.text}</p>
      ) : null}

      {servers === null ? <p className="detail">Loading…</p>
       : ordered.length === 0 ? (
         <p className="detail" data-testid="mcp-empty">
           None registered yet. Register one above, or ask BOSS to — it can register a server
           and bind one of its tools to a workflow step from the conversation.
         </p>
       ) : (
        <ul className="skill-list" data-testid="mcp-list">
          {ordered.map((s) => {
            const r = reach(s);
            const users = [
              ...(s.references?.custom_steps ?? []).map((x) => `step “${x.label || x.step_key}”`),
              ...(s.references?.skills ?? []).map((x) => `skill “${x.label || x.skill_key}”`),
            ];
            const holders = (s.references?.agents ?? []).map((a) => a.role_key);
            return (
              <li key={s.id} className="skill-row" data-enabled={s.enabled}
                  data-testid={`mcp-row-${s.id}`}>
                <span className="skill-row-main">
                  <b>{s.name}</b>
                  <span className={`cap-badge tone-${r.tone}`} data-testid={`mcp-reach-${s.id}`}>
                    {r.label}
                  </span>
                  <span className={`cap-badge tone-${s.risk_tier >= 2 ? "amber" : "muted"}`}>
                    {s.risk_tier >= 2 ? "needs a person" : `risk ${s.risk_tier}`}
                  </span>
                  {s.trusted ? (
                    <span className="cap-badge tone-muted"
                          title={s.provenance?.reviewed_by
                            ? `Reviewed by ${s.provenance.reviewed_by}` : undefined}>reviewed</span>
                  ) : <span className="cap-badge tone-pink">unreviewed</span>}
                  {s.data_boundary ? (
                    <span className="cap-badge tone-muted">{s.data_boundary}</span>
                  ) : null}
                </span>
                {s.description ? <span className="detail">{s.description}</span> : null}
                {/* The URL and the id, quietly: the URL tells two similar servers apart, and the
                    id is what an mcp_tool skill and a custom step store. */}
                <code className="detail">{s.url} · {s.id}</code>
                {s.status_note ? (
                  <span className="setup-bad" data-testid={`mcp-status-${s.id}`}>{s.status_note}</span>
                ) : null}
                {(s.tools ?? []).length > 0 ? (
                  <span className="detail" data-testid={`mcp-tools-${s.id}`}>
                    {s.reachable === false ? "Tools it offered when last reached: " : "Tools: "}
                    {(s.tools ?? []).map((t) => t.name).join(", ")}
                  </span>
                ) : s.reachable === true ? (
                  <span className="detail">It answers, and offers no tools.</span>
                ) : null}
                {users.length > 0 ? (
                  <span className="detail">Used by {users.join(", ")}.</span>
                ) : null}
                {holders.length > 0 ? (
                  <span className="detail" data-testid={`mcp-holders-${s.id}`}>
                    Held by {holders.join(", ")}.
                  </span>
                ) : null}
                <span className="skill-assign-actions">
                  <button type="button" className="linklike" disabled={busy}
                          data-testid={`mcp-edit-${s.id}`} onClick={() => startEdit(s)}>Edit</button>
                  <button type="button" className="linklike" disabled={busy}
                          data-testid={`mcp-toggle-${s.id}`}
                          onClick={() => void send(
                            { action: "mcp_update", server_id: s.id,
                              changes: { enabled: !s.enabled } },
                            s.enabled ? `${s.name} is off — no agent or step can call it.`
                                      : `${s.name} is on.`)}>
                    {s.enabled ? "Turn off" : "Turn on"}
                  </button>
                  {confirmDelete === s.id ? (
                    <span className="skill-assign-pick" data-testid={`mcp-delete-confirm-${s.id}`}>
                      <span className="detail">
                        Remove {s.name}?{holders.length > 0
                          ? ` This also takes it off ${holders.join(", ")}.` : ""} This cannot
                        be undone.
                      </span>
                      <button type="button" className="cap-btn danger" disabled={busy}
                              data-testid={`mcp-delete-yes-${s.id}`}
                              onClick={() => { setConfirmDelete(null);
                                // release_grants only after the question above named
                                // the agents that hold it.
                                void send({ action: "mcp_delete", server_id: s.id,
                                            release_grants: holders.length > 0 },
                                          `Removed ${s.name}.`); }}>Remove</button>
                      <button type="button" className="linklike"
                              onClick={() => setConfirmDelete(null)}>Keep it</button>
                    </span>
                  ) : (
                    <button type="button" className="linklike" disabled={busy}
                            data-testid={`mcp-delete-${s.id}`}
                            title={users.length > 0
                              ? "Still used — re-point or delete those first, or turn it off."
                              : undefined}
                            onClick={() => {
                              if (users.length > 0) {
                                // Said here, before the press, rather than as a 409 after it.
                                setNote({ ok: false, text: `${s.name} is still used by `
                                  + `${users.join(", ")}. Re-point or delete those first — `
                                  + "or turn the server off instead of removing it." });
                                return;
                              }
                              setConfirmDelete(s.id);
                            }}>Remove</button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
