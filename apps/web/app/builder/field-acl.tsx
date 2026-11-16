"use client";

/**
 * Field gates (BOSSAPP-1268). A builder sets who can read or change each field, then
 * opens the app as that person. The open pane renders the server payload only: a field
 * the open left out is not drawn, and a field that is not writable has no editor.
 *
 * The desk is a mineral ledger of stamp toggles. The open pane is a blueprint slip with
 * the person's name as a watermark. Read-only values sit on a steel plate with a clasp.
 */
import React, { useEffect, useMemo, useState } from "react";

type Field = { key: string; label: string; position?: number };
type Grant = {
  field_key: string;
  subject_kind: "user" | "role";
  subject_id: string;
  can_read: boolean;
  can_change: boolean;
};
type Member = { user_id: string; roles: string[] };
type Desk = {
  id: string;
  project_id: string;
  name: string;
  created_by?: string;
  fields: Field[];
  grants: Grant[];
  members: Member[];
  values: Record<string, unknown>;
};
type OpenField = { key: string; label: string; writable: boolean };
type Opened = {
  app_id: string;
  as_user: string;
  name?: string;
  fields: OpenField[];
  values: Record<string, unknown>;
};
type Subject = { kind: "user" | "role"; id: string };
type Gate = { read: boolean; change: boolean };

export function FieldAcl({ projectId = "", actor = "" }: { projectId?: string; actor?: string }) {
  const [app, setApp] = useState<Desk | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [gates, setGates] = useState<Record<string, Gate>>({});
  const [gateError, setGateError] = useState("");
  const [asUser, setAsUser] = useState("");
  const [opened, setOpened] = useState<Opened | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [writeError, setWriteError] = useState("");
  const [saving, setSaving] = useState(false);
  const [personDraft, setPersonDraft] = useState("");
  const [name, setName] = useState("");
  const [labels, setLabels] = useState<string[]>([""]);
  const [createError, setCreateError] = useState("");

  useEffect(() => {
    if (!projectId) { setLoading(false); return; }
    let live = true;
    setLoading(true);
    fetch(`/v1/field-acl/apps?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!live) return;
        if (!response.ok) {
          setLoadError(body.message || "The field gates did not load. Reload the page.");
          return;
        }
        const first = Array.isArray(body.apps) ? body.apps[0] as Desk | undefined : undefined;
        if (first?.id) adopt(first);
        else setApp(null);
      })
      .catch(() => { if (live) setLoadError("The field gates did not load. Reload the page."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [projectId]);

  function adopt(next: Desk) {
    setApp(next);
    setGates(gatesFrom(next));
    setAsUser((current) => current && next.members.some((m) => m.user_id === current)
      ? current : (next.members[0]?.user_id ?? ""));
    setLoadError("");
  }

  const subjects = useMemo(() => (app ? subjectsOf(app) : []), [app]);

  async function saveGates() {
    if (!app) return;
    setGateError("");
    setSaving(true);
    try {
      const response = await fetch(`/v1/field-acl/apps/${encodeURIComponent(app.id)}/grants`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, grants: grantsFrom(app, subjects, gates) }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) setGateError(body.message || "The gates were not saved.");
    } catch {
      setGateError("The gates were not saved.");
    } finally {
      setSaving(false);
    }
  }

  function toggle(field: Field, subject: Subject, which: "read" | "change") {
    const token = gateKey(field.key, subject);
    setGates((prev) => {
      const current = prev[token] ?? { read: false, change: false };
      const next = { ...current };
      if (which === "read") {
        next.read = !current.read;
        if (!next.read) next.change = false;
      } else {
        next.change = !current.change;
        if (next.change) next.read = true;
      }
      return { ...prev, [token]: next };
    });
  }

  async function openAs() {
    if (!app || !asUser) return;
    setWriteError("");
    const params = new URLSearchParams({ project_id: projectId, as_user: asUser, actor });
    try {
      const response = await fetch(
        `/v1/field-acl/apps/${encodeURIComponent(app.id)}/open?${params}`,
        { cache: "no-store" },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setOpened(null);
        setLoadError(body.message || "The app did not open.");
        return;
      }
      const view = body as Opened;
      setOpened(view);
      const next: Record<string, string> = {};
      for (const field of view.fields ?? []) {
        if (!field.writable) continue;
        const value = view.values?.[field.key];
        next[field.key] = value == null ? "" : String(value);
      }
      setDraft(next);
    } catch {
      setLoadError("The app did not open.");
    }
  }

  async function saveOpened() {
    if (!app || !opened) return;
    const values: Record<string, string> = {};
    for (const field of opened.fields) {
      if (!field.writable) continue;
      values[field.key] = draft[field.key] ?? "";
    }
    setWriteError("");
    setSaving(true);
    try {
      const response = await fetch(`/v1/field-acl/apps/${encodeURIComponent(app.id)}/record`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, as_user: opened.as_user, values }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setWriteError(body.message || "You can't change that field.");
        return;
      }
      setOpened(body as Opened);
    } catch {
      setWriteError("The save did not reach BOSS.");
    } finally {
      setSaving(false);
    }
  }

  async function addPerson() {
    if (!app) return;
    const userId = personDraft.trim();
    if (!userId) return;
    setGateError("");
    try {
      const response = await fetch(
        `/v1/field-acl/apps/${encodeURIComponent(app.id)}/members/${encodeURIComponent(userId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, roles: [] }),
        },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        setGateError(body.message || "That person was not added.");
        return;
      }
      setApp({
        ...app,
        members: [...app.members.filter((member) => member.user_id !== userId),
          { user_id: userId, roles: body.roles ?? [] }],
      });
      setPersonDraft("");
      setAsUser(userId);
    } catch {
      setGateError("That person was not added.");
    }
  }

  async function createApp() {
    const fields = labels
      .map((label) => label.trim())
      .filter(Boolean)
      .map((label) => ({ key: fieldKeyFromLabel(label), label }))
      .filter((field) => field.key);
    if (!name.trim()) { setCreateError("Name the app."); return; }
    if (!fields.length) { setCreateError("Add a field first."); return; }
    setCreateError("");
    try {
      const response = await fetch("/v1/field-acl/apps", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, actor, name: name.trim(), fields }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.id) {
        setCreateError(body.message || "The app was not created.");
        return;
      }
      adopt(body as Desk);
    } catch {
      setCreateError("The app was not created.");
    }
  }

  if (!projectId) {
    return <div className="fg1268" data-testid="field-acl"><style>{CSS}</style><p>Pick a project first.</p></div>;
  }

  const writable = (opened?.fields ?? []).some((field) => field.writable);

  return (
    <div className="fg1268" data-testid="field-acl">
      <style>{CSS}</style>
      <header className="fg1268-head">
        <p className="fg1268-eyebrow">Builder</p>
        <h1>Field gates</h1>
        <p className="fg1268-lead">
          Set who can read or change each field. Then open the app as that person.
        </p>
      </header>
      {loadError ? <p role="alert">{loadError}</p> : null}
      {loading ? <p>Loading field gates.</p> : null}
      {!loading && !app ? (
        <form className="fg1268-create" onSubmit={(event) => event.preventDefault()}>
          <p>Name the app, then the fields a person might see.</p>
          <label className="fg1268-line">
            <span>App name</span>
            <input aria-label="App name" value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          {labels.map((label, index) => (
            <label className="fg1268-line" key={index}>
              <span>Field</span>
              <input
                aria-label="Field label"
                value={label}
                onChange={(event) => setLabels(labels.map((item, i) => i === index ? event.target.value : item))}
              />
            </label>
          ))}
          <div className="fg1268-actions">
            <button type="button" onClick={() => setLabels([...labels, ""])}>Add field</button>
            <button type="button" data-testid="create-app" onClick={() => void createApp()}>Create app</button>
          </div>
          {createError ? <p role="alert">{createError}</p> : null}
        </form>
      ) : null}
      {app ? (
        <div className="fg1268-split">
          <section data-testid="gate-desk" aria-label={app.name}>
            <h2>{app.name}</h2>
            {app.fields.map((field) => (
              <div className="fg1268-field" data-field={field.key} key={field.key}>
                <div className="fg1268-field-head">
                  <h3>{field.label}</h3>
                  <span className="fg1268-key">{field.key}</span>
                </div>
                {app.values?.[field.key] != null ? (
                  <p className="fg1268-stored">{String(app.values[field.key])}</p>
                ) : null}
                {subjects.map((subject) => {
                  const gate = gates[gateKey(field.key, subject)] ?? { read: false, change: false };
                  return (
                    <div className="fg1268-who" key={subject.kind + subject.id}>
                      <span className="fg1268-name">{subject.id}</span>
                      <span className="fg1268-kind">{subject.kind === "role" ? "role" : "person"}</span>
                      <button
                        type="button"
                        className="fg1268-stamp"
                        data-kind="read"
                        aria-pressed={gate.read ? "true" : "false"}
                        aria-label={`${subject.id} can read ${field.label}`}
                        title="They can see this field"
                        onClick={() => toggle(field, subject, "read")}
                      >Read</button>
                      <button
                        type="button"
                        className="fg1268-stamp"
                        data-kind="change"
                        aria-pressed={gate.change ? "true" : "false"}
                        aria-label={`${subject.id} can change ${field.label}`}
                        title="They can edit this field"
                        onClick={() => toggle(field, subject, "change")}
                      >Change</button>
                    </div>
                  );
                })}
              </div>
            ))}
            <div className="fg1268-actions">
              <button type="button" data-testid="save-gates" onClick={() => void saveGates()} disabled={saving}>
                Save gates
              </button>
            </div>
            <div className="fg1268-add">
              <label>
                <span>Person</span>
                <input aria-label="Person" value={personDraft} onChange={(event) => setPersonDraft(event.target.value)} />
              </label>
              <button type="button" onClick={() => void addPerson()}>Add person</button>
            </div>
            {gateError ? <p role="alert">{gateError}</p> : null}
          </section>
          <section className="fg1268-slip" aria-label="Open as">
            <p className="fg1268-eyebrow">Open as</p>
            <div className="fg1268-open-row">
              <select data-testid="open-as" value={asUser} onChange={(event) => setAsUser(event.target.value)}>
                {app.members.map((member) => (
                  <option key={member.user_id} value={member.user_id}>{member.user_id}</option>
                ))}
              </select>
              <button type="button" data-testid="open-as-go" title="Show the app as this person" onClick={() => void openAs()}>
                {asUser ? `Open as ${asUser}` : "Open as"}
              </button>
            </div>
            <p>Open the app as this person.</p>
            {opened ? (
              <div className="fg1268-opened" data-testid="opened-app" data-as-user={opened.as_user}>
                <p className="fg1268-mark" aria-hidden="true">{opened.as_user}</p>
                <h2>{opened.as_user}</h2>
                {opened.fields.length === 0 ? <p>This person sees no fields.</p> : null}
                {opened.fields.map((field) => (
                  field.writable ? (
                    <label className="fg1268-edit" data-field={field.key} key={field.key}>
                      <span>{field.label}</span>
                      <input
                        name={field.key}
                        aria-label={field.label}
                        value={draft[field.key] ?? ""}
                        onChange={(event) => setDraft({ ...draft, [field.key]: event.target.value })}
                      />
                    </label>
                  ) : (
                    <div className="fg1268-locked" data-field={field.key} key={field.key}>
                      <div className="fg1268-locked-head">
                        <span>{field.label}</span>
                        <span className="fg1268-clasp" aria-hidden="true" />
                        <span>Read only</span>
                      </div>
                      <p className="fg1268-plate">{shown(opened.values?.[field.key])}</p>
                    </div>
                  )
                ))}
                {writable ? (
                  <button type="button" data-testid="save-opened" onClick={() => void saveOpened()} disabled={saving}>
                    Save changes
                  </button>
                ) : null}
                {writeError ? <p role="alert">{writeError}</p> : null}
              </div>
            ) : null}
          </section>
        </div>
      ) : null}
    </div>
  );
}

function shown(value: unknown): string {
  if (value == null || value === "") return "Empty";
  return String(value);
}

function gateKey(fieldKey: string, subject: Subject): string {
  return `${fieldKey}\n${subject.kind}\n${subject.id}`;
}

function gatesFrom(app: Desk): Record<string, Gate> {
  const gates: Record<string, Gate> = {};
  for (const grant of app.grants ?? []) {
    gates[`${grant.field_key}\n${grant.subject_kind}\n${grant.subject_id}`] = {
      read: Boolean(grant.can_read || grant.can_change),
      change: Boolean(grant.can_change),
    };
  }
  return gates;
}

function subjectsOf(app: Desk): Subject[] {
  const out: Subject[] = [];
  const seen = new Set<string>();
  const add = (kind: "user" | "role", id: string) => {
    const token = `${kind}:${id}`;
    if (!id || seen.has(token)) return;
    seen.add(token);
    out.push({ kind, id });
  };
  for (const member of app.members ?? []) {
    add("user", member.user_id);
    for (const role of member.roles ?? []) add("role", role);
  }
  for (const grant of app.grants ?? []) add(grant.subject_kind, grant.subject_id);
  return out;
}

function grantsFrom(app: Desk, subjects: Subject[], gates: Record<string, Gate>): Grant[] {
  const grants: Grant[] = [];
  for (const field of app.fields) {
    for (const subject of subjects) {
      const gate = gates[gateKey(field.key, subject)] ?? { read: false, change: false };
      if (!gate.read && !gate.change) continue;
      const read = gate.read || gate.change;
      grants.push({
        field_key: field.key,
        subject_kind: subject.kind,
        subject_id: subject.id,
        can_read: read,
        can_change: gate.change && read,
      });
    }
  }
  return grants;
}

function fieldKeyFromLabel(label: string): string {
  return label.trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 41);
}

const CSS = `
@import url("https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@500;600;700&family=Inconsolata:wght@500;700&display=swap");
.fg1268 {
  --paper: #D5E3DC;
  --ink: #172033;
  --strip: #F4F8F6;
  --line: #7E978C;
  --read: #1B655E;
  --seal: #8C2F2B;
  --slip: #E3EEF7;
  --slip-ink: #1B3148;
  --focus: #B86A0A;
  --plate: #D5DEE8;
  background: var(--paper);
  color: var(--ink);
  font-family: "Familjen Grotesk", "Avenir Next", "Segoe UI", sans-serif;
  padding: 28px 32px 56px;
  min-height: 100%;
  box-sizing: border-box;
}
.fg1268 * { box-sizing: border-box; }
.fg1268-head { max-width: 38rem; margin-bottom: 28px; }
.fg1268-eyebrow {
  font-family: "Inconsolata", "IBM Plex Mono", ui-monospace, monospace;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-size: 0.78rem;
  margin: 0 0 8px;
}
.fg1268 h1 {
  font-weight: 700;
  font-size: 3.1rem;
  letter-spacing: -0.035em;
  line-height: 0.92;
  margin: 0 0 12px;
}
.fg1268 h2 { font-size: 1.7rem; font-weight: 700; letter-spacing: -0.03em; margin: 0 0 8px; }
.fg1268-lead, .fg1268 p { line-height: 1.45; }
.fg1268-lead { margin: 0; max-width: 36rem; }
.fg1268-split {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(260px, 0.75fr);
  gap: 28px;
  align-items: start;
}
.fg1268-field { border-top: 1px solid var(--line); padding: 14px 0 12px; }
.fg1268-field-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.fg1268-field h3 { margin: 0; font-size: 1.35rem; font-weight: 650; letter-spacing: -0.02em; }
.fg1268-key, .fg1268-name, .fg1268-kind, .fg1268-stored, .fg1268-plate {
  font-family: "Inconsolata", ui-monospace, monospace;
}
.fg1268-key { color: #3C534A; font-size: 0.85rem; }
.fg1268-stored { margin: 4px 0 0; color: #3C534A; font-size: 0.92rem; }
.fg1268-who { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.fg1268-name { min-width: 6.5rem; }
.fg1268-kind { font-size: 0.75rem; letter-spacing: 0.08em; text-transform: uppercase; color: #3C534A; }
.fg1268-stamp {
  border: 1px solid var(--line);
  background: var(--strip);
  color: var(--ink);
  border-radius: 999px;
  padding: 4px 12px;
  font: inherit;
  font-size: 0.82rem;
  cursor: pointer;
}
.fg1268-stamp[data-kind="read"][aria-pressed="true"] { background: var(--read); border-color: var(--read); color: #F4FFFC; }
.fg1268-stamp[data-kind="change"][aria-pressed="true"] { background: var(--seal); border-color: var(--seal); color: #FFF7F5; }
.fg1268-actions, .fg1268-add, .fg1268-open-row { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; margin-top: 16px; }
.fg1268 button {
  font: inherit;
  cursor: pointer;
}
.fg1268-actions button, .fg1268-slip > button, .fg1268-opened > button, .fg1268-open-row button, .fg1268-add button, .fg1268-create button {
  background: var(--ink);
  color: #F4F8F6;
  border: 0;
  padding: 10px 16px;
  font-weight: 650;
}
.fg1268-slip {
  background: var(--slip);
  color: var(--slip-ink);
  padding: 22px 22px 28px;
  box-shadow: 8px 10px 0 var(--slip-ink);
}
.fg1268-slip select, .fg1268 input {
  font: inherit;
  color: inherit;
  background: transparent;
  border: 0;
  border-bottom: 2px solid currentColor;
  padding: 6px 0;
  min-width: 0;
}
.fg1268-slip select { min-width: 8rem; }
.fg1268-opened { position: relative; margin-top: 18px; overflow: hidden; }
.fg1268-mark {
  position: absolute;
  right: -6px;
  bottom: -22px;
  margin: 0;
  font-size: 6.4rem;
  font-weight: 700;
  letter-spacing: -0.05em;
  line-height: 0.8;
  color: rgba(27, 49, 72, 0.09);
  pointer-events: none;
  user-select: none;
}
.fg1268-edit, .fg1268-locked, .fg1268-line { display: block; margin-top: 14px; }
.fg1268-edit span, .fg1268-line span, .fg1268-locked-head { display: block; font-size: 0.82rem; letter-spacing: 0.08em; text-transform: uppercase; }
.fg1268-edit input { width: 100%; font-size: 1.2rem; }
.fg1268-locked-head { display: flex; align-items: center; gap: 8px; }
.fg1268-clasp {
  width: 12px;
  height: 14px;
  border: 2px solid var(--slip-ink);
  border-radius: 1px 1px 6px 6px;
  position: relative;
  display: inline-block;
}
.fg1268-clasp::before {
  content: "";
  position: absolute;
  top: -7px;
  left: 1px;
  width: 6px;
  height: 7px;
  border: 2px solid var(--slip-ink);
  border-bottom: 0;
  border-radius: 6px 6px 0 0;
}
.fg1268-plate { background: var(--plate); margin: 8px 0 0; padding: 10px 12px; font-size: 1rem; }
.fg1268 [role="alert"] { color: var(--seal); font-weight: 650; }
.fg1268 button:focus-visible, .fg1268 input:focus-visible, .fg1268 select:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
@media (max-width: 840px) {
  .fg1268 { padding: 20px 16px 40px; }
  .fg1268-split { display: block; }
  .fg1268-slip { margin-top: 22px; }
  .fg1268 h1 { font-size: 2.4rem; }
}
@media (prefers-reduced-motion: reduce) {
  .fg1268-slip, .fg1268-plate, .fg1268-stamp { transition: none; }
}
`;
