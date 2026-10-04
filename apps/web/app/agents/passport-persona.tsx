"use client";

/**
 * Profile › Voice and Face (spec §3.4 › Profile › Persona).
 *
 * How the agent speaks — never what it may do. Everything here is compiled into every prompt the
 * agent gets (L3 voice, the persona compiler), so a slider moved here is heard in the dock, in
 * a panel and in Test. The server lints it: a voice that tries to grant itself a tool or override
 * its prompt is refused with `Persona can't change what the agent may do`, and the field it
 * names is marked here. A real person's name is a warning, not a refusal; the agent still never
 * claims to be them. The footer line saying it is an AI cannot be edited.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Sparkle, X } from "@phosphor-icons/react";

import type { Passport, Persona, Voice } from "../../lib/agent-world-types";
import {
  ARCHETYPE_KEYS, DISCLOSURE, VOICE_AXES, compactPersona, draftPersonaField, fullPersona,
  identityOf, resolveAvatar, savePersona, toast,
} from "../../lib/agent-world";
import { AgFace } from "./agent-face";
import { FacePicker } from "./face-picker";

export const LIMITS = { callsign: 16, tagline: 60, about_me: 280, phrases: 3, never: 5 } as const;

/** Client-side bounds, the same the server enforces: said before Save, not after. */
export function personaProblems(p: Persona): Record<string, string> {
  const out: Record<string, string> = {};
  if ((p.callsign ?? "").length > LIMITS.callsign) out.callsign = `${LIMITS.callsign} characters at most`;
  if ((p.tagline ?? "").length > LIMITS.tagline) out.tagline = `${LIMITS.tagline} characters at most`;
  if ((p.about_me ?? "").length > LIMITS.about_me) out.about_me = `${LIMITS.about_me} characters at most`;
  return out;
}

function Chips({ label, values, max, onChange, testid, placeholder }: {
  label: string; values: string[]; max: number; onChange: (v: string[]) => void; testid: string;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (!v || values.includes(v) || values.length >= max) return;
    onChange([...values, v]);
    setDraft("");
  };
  return (
    <div className="ag-field" data-testid={testid}>
      <span className="ag-field-label">{label} <span className="ag-muted">{values.length}/{max}</span></span>
      <div className="ag-chips">
        {values.map((v) => (
          <span key={v} className="ag-chip" data-user-text>
            {v}
            <button type="button" aria-label={`Remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>
              <X size={10} aria-hidden />
            </button>
          </span>
        ))}
        {values.length < max ? (
          <input value={draft} placeholder={placeholder} aria-label={label}
                 onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
                 onBlur={add} />
        ) : null}
      </div>
    </div>
  );
}

export function PassportPersona({ projectId, actor, roleKey, passport, onSaved, onDraft }: {
  projectId: string; actor: string; roleKey: string; passport: Passport | null;
  onSaved: (version: number, fingerprint: string, persona: Persona) => void;
  /** The unsaved voice, so Test can be heard before Save. */
  onDraft?: (p: Persona | null) => void;
}) {
  const saved = useMemo(() => fullPersona(passport?.persona), [passport]);
  const [p, setP] = useState<Persona>(saved);
  const [busy, setBusy] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [warnings, setWarnings] = useState<string[]>([]);
  useEffect(() => { setP(saved); setErrors({}); }, [saved]);

  const dirty = JSON.stringify(compactPersona(p)) !== JSON.stringify(compactPersona(saved));
  useEffect(() => { onDraft?.(dirty ? compactPersona(p) : null); }, [dirty, p, onDraft]);
  const problems = personaProblems(p);
  const set = <K extends keyof Persona>(k: K, v: Persona[K]) => setP((x) => ({ ...x, [k]: v }));
  const setVoice = (k: keyof Voice, v: number) => setP((x) => ({ ...x, voice: { ...x.voice!, [k]: v } }));
  const who = identityOf(roleKey);
  // The face as every chip draws it; the picker edits that, and only a change is saved.
  const face = resolveAvatar(who.roleKey || roleKey, passport?.agentId || who.agentId, p.avatar);

  const draft = async () => {
    setBusy("draft");
    const r = await draftPersonaField(projectId, actor, roleKey, "about_me", compactPersona(p));
    setBusy("");
    if (!r.ok) {
      toast("error", r.status === 409 ? "Needs a model. Set one in Settings." : r.error);
      return;
    }
    set("about_me", r.data.slice(0, LIMITS.about_me));
  };

  const save = async () => {
    if (Object.keys(problems).length) { setErrors(problems); return; }
    setBusy("save"); setErrors({}); setWarnings([]);
    const r = await savePersona(projectId, actor, roleKey, compactPersona(p));
    setBusy("");
    if (!r.ok) {
      const field = (r as { field?: string }).field;
      if (field) setErrors({ [field.replace(/^persona\./, "")]: r.error });
      else setErrors({ form: r.error });
      return;
    }
    setWarnings(r.data.warnings);
    onSaved(r.data.version, r.data.fingerprint, compactPersona(p));
    toast("success", `Saved · v${r.data.version} · applies to new work`);
  };

  const err = (k: string) => (errors[k] ? <span className="ag-bad" role="alert">{errors[k]}</span> : null);

  return (
    <form className="ag-persona" data-testid="ag-persona" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <div className="ag-field">
        <span className="ag-field-label">Archetype</span>
        <div className="ag-arch-grid" role="radiogroup" aria-label="Archetype">
          {ARCHETYPE_KEYS.map((k) => {
            const a = identityOf(k);
            return (
              <button key={k} type="button" role="radio" aria-checked={p.archetype === k}
                      className="ag-arch" data-testid={`ag-arch-${k}`}
                      title={`Inspired by a way of thinking${a.tagline ? ` · ${a.tagline}` : ""}`}
                      onClick={() => set("archetype", k)}>
                <AgFace roleKey={k} identity={a} size={32} decorative />
                <span>{a.callsign || a.name}</span>
              </button>
            );
          })}
          <button type="button" role="radio" aria-checked={!p.archetype} className="ag-arch"
                  data-testid="ag-arch-custom" onClick={() => set("archetype", null)}>
            <span className="ag-arch-custom" aria-hidden>+</span>
            <span>Custom</span>
          </button>
        </div>
      </div>

      <div className="ag-row">
        <label className="ag-field">
          <span className="ag-field-label">Callsign</span>
          <input value={p.callsign} maxLength={LIMITS.callsign} placeholder="Vera" data-testid="ag-callsign"
                 onChange={(e) => set("callsign", e.target.value)} />
          {err("callsign")}
        </label>
        <label className="ag-field ag-grow">
          <span className="ag-field-label">Tagline <span className="ag-muted">{(p.tagline ?? "").length}/{LIMITS.tagline}</span></span>
          <input value={p.tagline} placeholder="Show me the test." data-testid="ag-tagline"
                 onChange={(e) => set("tagline", e.target.value)} />
          {err("tagline")}
        </label>
      </div>

      <label className="ag-field">
        <span className="ag-field-label">
          About me <span className="ag-muted">{(p.about_me ?? "").length}/{LIMITS.about_me}</span>
          <button type="button" className="ag-link" disabled={!!busy} data-testid="ag-draft-about"
                  title="One model call in its voice. Not saved until Save."
                  onClick={() => void draft()}>
            <Sparkle size={12} aria-hidden /> {busy === "draft" ? "Drafting…" : "Draft it"}
          </button>
        </span>
        <textarea rows={3} value={p.about_me} placeholder="In first person" data-testid="ag-about-me"
                  onChange={(e) => set("about_me", e.target.value)} />
        {err("about_me")}
      </label>

      <fieldset className="ag-field ag-voice" title="Changes how it speaks. Never what it may do.">
        <legend className="ag-field-label">Voice</legend>
        {VOICE_AXES.map(({ key, low, high }) => (
          <label key={key} className="ag-slider">
            <span className="ag-muted">{low}</span>
            <input type="range" min={0} max={4} step={1} value={p.voice?.[key] ?? 2}
                   aria-label={`${low} to ${high}`} data-testid={`ag-voice-${key}`}
                   onChange={(e) => setVoice(key, Number(e.target.value))} />
            <span className="ag-muted">{high}</span>
          </label>
        ))}
        {err("voice")}
      </fieldset>

      <Chips label="Says things like" values={p.phrases ?? []} max={LIMITS.phrases} testid="ag-phrases"
             placeholder="Add a phrase" onChange={(v) => set("phrases", v)} />
      {err("phrases")}
      <Chips label="Will never" values={p.never ?? []} max={LIMITS.never} testid="ag-never"
             placeholder="Add a rule" onChange={(v) => set("never", v)} />
      {err("never")}

      <div className="ag-field" data-testid="ag-face-editor">
        <span className="ag-field-label">Face</span>
        <FacePicker value={face} name={who.callsign || who.name}
                    onChange={(next) => set("avatar", next)} />
        {err("avatar")}
      </div>

      {warnings.map((w) => <div key={w} className="ag-warn" role="status" data-testid="ag-persona-warning">{w}</div>)}
      {errors.form ? <div className="ag-bad" role="alert" data-testid="ag-persona-error">{errors.form}</div> : null}

      <footer className="ag-persona-foot">
        <span className="ag-disclosure" data-testid="ag-disclosure">{DISCLOSURE}</span>
        <span className="ag-spacer" />
        {dirty ? (
          <button type="button" className="ag-btn" disabled={!!busy} onClick={() => { setP(saved); setErrors({}); }}>
            Undo
          </button>
        ) : null}
        <button type="submit" className="ag-btn ag-btn-primary" disabled={!dirty || !!busy || !passport}
                data-testid="ag-persona-save">{busy === "save" ? "Saving…" : "Save"}</button>
      </footer>
    </form>
  );
}
