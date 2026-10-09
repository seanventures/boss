"use client";

/**
 * "TALKS TO" (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): in the composer, the other apps of this
 * project the new app (or the app a feature goes into) should call. Each row: the app, the alias
 * its env vars are named by (BOSS_LINK_<ALIAS>_URL / _USERS / _TOKEN), Preview or Deployed, and
 * whether it gets that app's generated test users.
 *
 * The composer decides WHEN the links are made (an existing app: before the change starts, so its
 * build already has them; a new app: the start records them and its first build makes them).
 * This file only draws the rows. Gated on `app_link:manage`: without it the field stays visible,
 * greyed, with the reason (the composer passes its gate in, so the press that sends the links
 * and the field that collects them read one gate).
 */
// React by name: the composer's suite renders with the classic runtime.
import React from "react";

import { HelpTip } from "../help-tip";
import type { Gate } from "../../components/gated";

export const TALKS_TO_TIP = "B gets A's address and test logins at build and run time.";

export type TalksToRow = { provider: string; alias: string; env: "preview" | "deploy"; testUsers: boolean;
                           /** The alias was typed by the person: a new app choice no longer rewrites it. */
                           aliasTyped?: boolean };

export type LinkRequest = { provider_app_key: string; alias: string; env: "preview" | "deploy";
                            credentials: "test_users" | "none" };

const ALIAS = /^[A-Z][A-Z0-9_]{1,30}$/;

/** An alias from an app's name: "Bank attacker" → BANK_ATTACKER; never longer than the API takes. */
export function aliasFor(name: string): string {
  let a = name.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (!/^[A-Z]/.test(a)) a = `APP_${a}`.replace(/_+$/, "");
  return a.slice(0, 31).replace(/_+$/, "") || "APP";
}

/** What is wrong with the rows, in words ("" when they can be sent). */
export function talksToProblem(rows: TalksToRow[]): string {
  const seen = new Set<string>();
  for (const r of rows) {
    if (!r.provider) return "Talks to: pick the app, or remove the row.";
    if (!ALIAS.test(r.alias)) return `Talks to: ${r.alias || "an alias"} must be A-Z, 0-9 or _, 2-31 long.`;
    const k = `${r.alias}/${r.env}`;
    if (seen.has(k)) return `Talks to: ${r.alias} is used twice.`;
    seen.add(k);
  }
  return "";
}

/** The body each row becomes: POST /v1/apps/{app}/links, or one entry of a start's `app_links`. */
export function linkRequests(rows: TalksToRow[]): LinkRequest[] {
  return rows.map((r) => ({ provider_app_key: r.provider, alias: r.alias, env: r.env,
                            credentials: r.testUsers ? "test_users" : "none" }));
}

export function TalksTo({ apps, rows, onChange, gate }: {
  /** The apps it may talk to: this project's, less the app itself. */
  apps: { key: string; app: string }[];
  rows: TalksToRow[];
  onChange: (rows: TalksToRow[]) => void;
  gate: Gate;
}) {
  if (!apps.length) return null;                  // nothing in this project to talk to yet
  const set = (i: number, patch: Partial<TalksToRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const nameOf = (key: string) => apps.find((a) => a.key === key)?.app ?? "";
  return (
    <div data-testid="np-talks-to">
      <div className="np-lbl">Talks to <em>optional</em>
        <HelpTip label="Talks to" text={`${TALKS_TO_TIP} Each link names the other app by an alias: BOSS_LINK_<ALIAS>_URL.`} /></div>
      {rows.length ? (
        <div className="np-talks">
          {rows.map((r, i) => (
            <div className="np-talk" key={i} data-testid={`np-talk-${i}`}>
              <select className="mr-select np-talk-app" value={r.provider} aria-label="App it talks to"
                      data-testid={`np-talk-app-${i}`}
                      onChange={(e) => set(i, { provider: e.target.value,
                                                ...(r.aliasTyped ? {} : { alias: aliasFor(nameOf(e.target.value)) }) })}>
                <option value="">Choose an app…</option>
                {apps.map((a) => <option key={a.key} value={a.key}>{a.app}</option>)}
              </select>
              <input className="input np-talk-alias" value={r.alias} aria-label="Alias" data-testid={`np-talk-alias-${i}`}
                     title={`Its env vars are BOSS_LINK_${r.alias || "<ALIAS>"}_URL, _USERS and _TOKEN`}
                     placeholder="BANK" maxLength={31}
                     onChange={(e) => set(i, { alias: e.target.value.toUpperCase(), aliasTyped: true })} />
              <select className="mr-select np-talk-env" value={r.env} aria-label="Reaches its" data-testid={`np-talk-env-${i}`}
                      title="Which of its addresses: its live preview, or its deployed app"
                      onChange={(e) => set(i, { env: e.target.value === "deploy" ? "deploy" : "preview" })}>
                <option value="preview">Preview</option>
                <option value="deploy">Deployed</option>
              </select>
              <label className="np-talk-users" title="Generated test-only logins of that app, rotated on each new preview">
                <input type="checkbox" checked={r.testUsers} data-testid={`np-talk-users-${i}`}
                       onChange={(e) => set(i, { testUsers: e.target.checked })} /> Test users
              </label>
              <button type="button" className="np-rm" aria-label={`Remove ${r.alias || "this link"}`}
                      data-testid={`np-talk-rm-${i}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>✕</button>
            </div>
          ))}
        </div>
      ) : null}
      <button type="button" className="np-add" data-testid="np-talk-add"
              title={gate.allowed ? TALKS_TO_TIP : gate.reason} {...gate.props}
              onClick={gate.guard(() => onChange([...rows, { provider: "", alias: "", env: "preview", testUsers: true }]))}>
        + Link an app{gate.sr}
      </button>
    </div>
  );
}
