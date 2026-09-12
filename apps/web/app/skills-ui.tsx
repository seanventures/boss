"use client";

/**
 * Skills, as objects you author rather than ids you enable.
 *
 * The thing most worth customising is the thing BOSS knew least about: the operator's own
 * tools, accounts and house rules. Behaviour used to live in a Python executor inside the
 * product, so every capability a customer wanted was a pull request. A skill authored here
 * attaches at both ends that already existed — an agent's requested skills, and a workflow's
 * custom step — so nothing downstream had to be invented to make one reusable.
 *
 * The form is deliberately plain and the knowledge box is deliberately large. Knowledge is the
 * part an operator will spend real time on: the format their team uses, the clauses that
 * matter, the thing that makes the skill behave the same on its hundredth run.
 */

import React, { useCallback, useEffect, useState } from "react";

type Skill = {
  skill_key: string; label: string; summary: string; behaviour_kind: string;
  knowledge: string; inputs: Array<{ name: string; required?: boolean }>;
  produces: Array<{ name: string }>; config: Record<string, unknown>;
  risk_tier: number; trusted: boolean; enabled: boolean; requires_approval: boolean;
};

const BEHAVIOURS = [
  { id: "sandboxed_command", label: "Runs a command in a sandbox",
    hint: "ffmpeg, pandoc, ImageMagick — anything with a command line. Needs a digest-pinned "
        + "image and real isolation to execute." },
  { id: "mcp_tool", label: "Calls an MCP tool",
    hint: "The answer for a third-party account: the MCP server holds the integration, the "
        + "skill names one of its tools." },
  { id: "builtin", label: "Names a built-in executor",
    hint: "One BOSS already ships. Use this to give an existing skill knowledge and a risk "
        + "tier without changing its code." },
];

const BLANK = {
  skill_key: "", label: "", summary: "", behaviour_kind: "sandboxed_command",
  knowledge: "", inputs: "", produces: "",
  image: "", command: "", timeout_seconds: "300", network: false,
  mcp_server_id: "", tool_name: "", executor_id: "",
  risk_tier: "1", trusted: false,
};

export function SkillsHome({ projectId, actor }: { projectId: string; actor: string }) {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [draft, setDraft] = useState({ ...BLANK });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/skills?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setSkills(((await r.json()).skills ?? []) as Skill[]);
      else setSkills([]);
    } catch { setSkills([]); }
  }, [projectId]);

  useEffect(() => { void reload(); }, [reload]);

  const save = useCallback(async () => {
    if (busy) return;
    setBusy(true); setNote(null);
    // Names, one per line or comma-separated — whichever an operator happens to type.
    const names = (raw: string) => raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
      .map((name) => ({ name }));
    const config: Record<string, unknown> =
      draft.behaviour_kind === "sandboxed_command"
        ? { image: draft.image.trim(), command: draft.command.trim(),
            timeout_seconds: Number(draft.timeout_seconds) || 300,
            network: draft.network, outputs: names(draft.produces) }
        : draft.behaviour_kind === "mcp_tool"
          ? { mcp_server_id: draft.mcp_server_id.trim(), tool_name: draft.tool_name.trim() }
          : { executor_id: draft.executor_id.trim() };
    try {
      const r = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId, actor,
          skill_key: draft.skill_key.trim(), label: draft.label.trim(),
          summary: draft.summary.trim(), behaviour_kind: draft.behaviour_kind,
          knowledge: draft.knowledge, inputs: names(draft.inputs),
          produces: names(draft.produces), config,
          risk_tier: Number(draft.risk_tier) || 1, trusted: draft.trusted,
        }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) {
        // The API's messages are already written for the operator, so they are shown as-is.
        setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) });
        return;
      }
      setNote({ ok: true, text: `Saved ${draft.skill_key.trim()}.` });
      setDraft({ ...BLANK });
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [busy, draft, projectId, actor, reload]);

  const remove = useCallback(async (key: string) => {
    setBusy(true); setNote(null);
    try {
      const r = await fetch("/api/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", skill_key: key, project_id: projectId, actor }),
      });
      const data = await r.json().catch(() => ({}));
      setBusy(false);
      if (!r.ok) { setNote({ ok: false, text: String(data?.error ?? `HTTP ${r.status}`) }); return; }
      void reload();
    } catch (e) {
      setBusy(false);
      setNote({ ok: false, text: String((e as Error)?.message ?? e) });
    }
  }, [projectId, actor, reload]);

  const chosen = BEHAVIOURS.find((b) => b.id === draft.behaviour_kind);

  return (
    <section className="panel-stack" aria-label="Skills">
      <article className="stack-card">
        <div className="section-head">
          <div>
            <span className="eyebrow">Skills</span>
            <h2>What your agents and steps can actually do.</h2>
            <p className="detail">
              A skill is one reusable unit of work. Attach it to an agent, or place it in a
              workflow as a step — the same object either way.
            </p>
          </div>
        </div>
        {skills === null ? <p className="detail">Loading…</p>
         : skills.length === 0 ? (
           <p className="detail" data-testid="skills-empty">
             Nothing authored yet. The form below is the whole of it.
           </p>
         ) : (
          <ul className="skill-list" data-testid="skill-list">
            {skills.map((s) => (
              <li key={s.skill_key} className="skill-row" data-enabled={s.enabled}>
                <span className="skill-row-main">
                  <b>{s.label}</b>
                  <code className="detail">{s.skill_key}</code>
                  <span className="cap-badge tone-muted">{s.behaviour_kind.replace(/_/g, " ")}</span>
                  {/* THE CONSEQUENCE, ON THE ROW. Tier alone means nothing to a reader; "needs
                      a person" is the fact they act on. */}
                  {s.requires_approval ? (
                    <span className="cap-badge tone-amber">needs a person</span>
                  ) : null}
                  {!s.trusted && s.requires_approval ? (
                    <span className="cap-badge tone-pink" title="An untrusted skill with this
                      much reach cannot be placed in a workflow until somebody reviews it.">
                      unreviewed
                    </span>
                  ) : null}
                </span>
                {s.summary ? <span className="detail">{s.summary}</span> : null}
                <button type="button" className="linklike" disabled={busy}
                        data-testid={`skill-delete-${s.skill_key}`}
                        onClick={() => void remove(s.skill_key)}>Delete</button>
              </li>
            ))}
          </ul>
        )}
      </article>

      <article className="stack-card" data-testid="skill-form">
        <div className="section-head">
          <div>
            <span className="eyebrow">Author a skill</span>
            <h2>{draft.skill_key ? `Editing ${draft.skill_key}` : "New skill"}</h2>
          </div>
        </div>
        <div className="setup-form">
          <label className="setup-field">
            <span>Name</span>
            <input type="text" value={draft.label} data-testid="skill-label"
                   placeholder="Trim a clip"
                   onChange={(e) => setDraft((d) => ({ ...d, label: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Key</span>
            <input type="text" value={draft.skill_key} data-testid="skill-key"
                   placeholder="trim_clip" spellCheck={false}
                   onChange={(e) => setDraft((d) => ({ ...d, skill_key: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>What it does</span>
            <input type="text" value={draft.summary} data-testid="skill-summary"
                   placeholder="Cuts a clip out of a longer video."
                   onChange={(e) => setDraft((d) => ({ ...d, summary: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>How it runs</span>
            <select value={draft.behaviour_kind} data-testid="skill-behaviour"
                    onChange={(e) => setDraft((d) => ({ ...d, behaviour_kind: e.target.value }))}>
              {BEHAVIOURS.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
            </select>
          </label>
          {chosen ? <p className="detail">{chosen.hint}</p> : null}

          {draft.behaviour_kind === "sandboxed_command" ? (
            <>
              <label className="setup-field">
                <span>Image (pinned by digest)</span>
                <input type="text" value={draft.image} data-testid="skill-image"
                       spellCheck={false} placeholder="ghcr.io/acme/ffmpeg@sha256:…"
                       onChange={(e) => setDraft((d) => ({ ...d, image: e.target.value }))} />
              </label>
              <label className="setup-field">
                <span>Command</span>
                <textarea rows={3} value={draft.command} data-testid="skill-command"
                          spellCheck={false}
                          placeholder="ffmpeg -i {video} -ss {start} -to {end} -c copy {workdir}/clip.mp4"
                          onChange={(e) => setDraft((d) => ({ ...d, command: e.target.value }))} />
              </label>
              {/* SAID BEFORE THEY WRITE IT, not after the save fails. Both of these are
                  structural and neither is obvious from a command box. */}
              <p className="detail">
                Every <code>{"{name}"}</code> must be one of the inputs below, and each one is
                shell-quoted — an input cannot become a second command. <code>{"{workdir}"}</code>
                {" "}is always available.
              </p>
              <label className="setup-field">
                <span>Network</span>
                <select value={draft.network ? "yes" : "no"} data-testid="skill-network"
                        onChange={(e) => setDraft((d) => ({ ...d, network: e.target.value === "yes" }))}>
                  <option value="no">Off — the command cannot reach anything</option>
                  <option value="yes">On — it needs to fetch or send</option>
                </select>
              </label>
            </>
          ) : draft.behaviour_kind === "mcp_tool" ? (
            <>
              <label className="setup-field">
                <span>MCP server id</span>
                <input type="text" value={draft.mcp_server_id} data-testid="skill-mcp-server"
                       spellCheck={false}
                       onChange={(e) => setDraft((d) => ({ ...d, mcp_server_id: e.target.value }))} />
              </label>
              <label className="setup-field">
                <span>Tool name</span>
                <input type="text" value={draft.tool_name} data-testid="skill-mcp-tool"
                       spellCheck={false}
                       onChange={(e) => setDraft((d) => ({ ...d, tool_name: e.target.value }))} />
              </label>
            </>
          ) : (
            <label className="setup-field">
              <span>Executor id</span>
              <input type="text" value={draft.executor_id} data-testid="skill-executor"
                     spellCheck={false} placeholder="deployment.health.check"
                     onChange={(e) => setDraft((d) => ({ ...d, executor_id: e.target.value }))} />
            </label>
          )}

          <label className="setup-field">
            <span>Inputs — one per line</span>
            <textarea rows={3} value={draft.inputs} data-testid="skill-inputs"
                      placeholder={"video\nstart\nend"}
                      onChange={(e) => setDraft((d) => ({ ...d, inputs: e.target.value }))} />
          </label>
          <label className="setup-field">
            <span>Produces — one per line</span>
            <textarea rows={2} value={draft.produces} data-testid="skill-produces"
                      placeholder="clip"
                      onChange={(e) => setDraft((d) => ({ ...d, produces: e.target.value }))} />
          </label>

          {/* THE PART WORTH THE MOST TIME. A binding says which tool runs; it says nothing
              about the standard the work is held to, and that knowledge used to live in the
              head of whoever defined the step. */}
          <label className="setup-field">
            <span>What this skill knows</span>
            <textarea rows={8} value={draft.knowledge} data-testid="skill-knowledge"
                      placeholder={"Markdown. The house rules this work is held to —\n"
                        + "the format your team uses, what must never happen,\n"
                        + "the internal doc that explains the system it touches.\n\n"
                        + "Carried into every execution, so the skill behaves the same on its\n"
                        + "hundredth run as on the one somebody watched."}
                      onChange={(e) => setDraft((d) => ({ ...d, knowledge: e.target.value }))} />
          </label>

          <label className="setup-field">
            <span>Risk</span>
            <select value={draft.risk_tier} data-testid="skill-risk"
                    onChange={(e) => setDraft((d) => ({ ...d, risk_tier: e.target.value }))}>
              <option value="0">0 — reads only</option>
              <option value="1">1 — writes something recoverable</option>
              <option value="2">2 — changes the world (always needs a person)</option>
              <option value="3">3 — irreversible and public</option>
            </select>
          </label>
          {Number(draft.risk_tier) >= 2 ? (
            <p className="detail" data-testid="skill-risk-note">
              At this tier a step using it cannot be set to auto — BOSS forces a human
              approval — and it must be marked reviewed before it can be placed at all.
            </p>
          ) : null}
          <label className="setup-field">
            <span>Reviewed</span>
            <select value={draft.trusted ? "yes" : "no"} data-testid="skill-trusted"
                    onChange={(e) => setDraft((d) => ({ ...d, trusted: e.target.value === "yes" }))}>
              <option value="no">Not yet reviewed</option>
              <option value="yes">A person has read this and vouches for it</option>
            </select>
          </label>

          <button type="button" className="cap-btn primary" data-testid="skill-save"
                  disabled={busy || !draft.skill_key.trim() || !draft.label.trim()}
                  onClick={() => void save()}>
            {busy ? "Saving…" : "Save skill"}
          </button>
        </div>
        {note ? (
          <p className={note.ok ? "setup-ok" : "setup-bad"} data-testid="skill-note">
            {note.text}
          </p>
        ) : null}
      </article>
    </section>
  );
}
