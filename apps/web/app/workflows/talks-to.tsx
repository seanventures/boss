"use client";

/**
 * "TALKS TO" IN THE COMPOSER (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): app B, the one being built,
 * gets app A's address and test logins at build and run time. An app picker (the project's other
 * apps: links are same-project only, D7) · Preview / Deployed · Test users, and the alias whose
 * env vars B reads (`BOSS_LINK_<ALIAS>_URL`, `_USERS`).
 *
 * The link is made once the start is accepted (POST /v1/apps/{B}/links): for an app that exists,
 * B is its build; for a new app, B is named by the title the start sends (`new_app`), so the link
 * is in place before its first build asks for it. Gated on app_link:manage, greyed with the reason.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { callAction } from "../../lib/call-action";
import type { ActionResult } from "../../lib/action-result";
import { parseApps, type AppRow } from "../factory/apps-tab";
import { createLinkAction } from "../factory/links-actions";
import { LINKS_TIP } from "../factory/links-sheet";
import { aliasFor, aliasProblem } from "../../lib/link-alias";

export { aliasFor, aliasProblem } from "../../lib/link-alias";

export type TalksTo = { provider: string; providerName: string; env: "preview" | "deploy"; testUsers: boolean; alias: string };

/** Make the link once the start was accepted. `consumer` is B's build (an existing app) or its
 *  title (a new app). */
export async function linkAfterStart(projectId: string, consumer: string, newApp: boolean, t: TalksTo): Promise<ActionResult> {
  const f = new FormData();
  f.set("project_id", projectId);
  f.set("app_key", consumer);
  f.set("provider_app_key", t.provider);
  f.set("alias", t.alias);
  f.set("env", t.env);
  if (t.testUsers) f.set("test_users", "on");
  if (newApp) f.set("new_app", "1");
  return callAction(createLinkAction, undefined, "app_link:manage")(f);
}

export function TalksToField({ projectId, exclude, value, onChange }: {
  projectId: string;
  /** The app being added to (its row key), never offered as its own provider. */
  exclude?: string | null;
  value: TalksTo | null;
  onChange: (v: TalksTo | null) => void;
}) {
  const [open, setOpen] = useState(!!value);
  const [apps, setApps] = useState<AppRow[] | null>(null);
  useEffect(() => {
    if (!open || apps) return;
    let alive = true;
    void (async () => {
      try {
        const r = await fetch(`/v1/factory/apps?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
        if (alive) setApps(r.ok ? parseApps(await r.json()) : []);
      } catch { if (alive) setApps([]); }
    })();
    return () => { alive = false; };
  }, [open, apps, projectId]);
  const others = (apps ?? []).filter((a) => a.key !== exclude && !a.builds.some((b) => b.runId === exclude));
  if (!open) {
    return (
      <GatedButton permission="app_link:manage" projectId={projectId} type="button" className="np-add"
                   data-testid="np-talks-open" title={LINKS_TIP} onClick={() => setOpen(true)}>+ Talks to another app</GatedButton>
    );
  }
  const set = (patch: Partial<TalksTo>) => {
    const base: TalksTo = value ?? { provider: "", providerName: "", env: "preview", testUsers: true, alias: "" };
    onChange({ ...base, ...patch });
  };
  const problem = value?.provider ? aliasProblem(value.alias) : "";
  return (
    <div className="np-talks" data-testid="np-talks">
      <div className="np-two">
        <label className="dw-field"><span className="dw-field-label">App</span>
          <select className="input" value={value?.provider ?? ""} data-testid="np-talks-app" aria-label="Talks to"
                  onChange={(e) => {
                    const a = others.find((x) => x.key === e.target.value);
                    if (!a) { onChange(null); return; }
                    set({ provider: a.key, providerName: a.app, alias: value?.alias && value.provider ? value.alias : aliasFor(a.app) });
                  }}>
            <option value="">{apps === null ? "Loading…" : others.length ? "None" : "No other app in this project"}</option>
            {others.map((a) => <option key={a.key} value={a.key}>{a.app}</option>)}
          </select>
        </label>
        <label className="dw-field"><span className="dw-field-label" title="Its env vars are BOSS_LINK_<ALIAS>_URL and _USERS">Alias</span>
          <input className="input" value={value?.alias ?? ""} disabled={!value?.provider} placeholder="BANKING" maxLength={31}
                 data-testid="np-talks-alias" aria-invalid={!!problem || undefined}
                 onChange={(e) => set({ alias: e.target.value.toUpperCase() })} />
        </label>
      </div>
      <div className="np-talks-opts">
        <label className="dw-field"><span className="dw-field-label">Reaches its</span>
          <select className="input" value={value?.env ?? "preview"} disabled={!value?.provider} data-testid="np-talks-env"
                  onChange={(e) => set({ env: e.target.value === "deploy" ? "deploy" : "preview" })}>
            <option value="preview">Preview</option>
            <option value="deploy">Deployed</option>
          </select>
        </label>
        <label className="dw-check" title="Generated, test-only logins, rotated on each new preview">
          <input type="checkbox" checked={value?.testUsers ?? true} disabled={!value?.provider} data-testid="np-talks-users"
                 onChange={(e) => set({ testUsers: e.target.checked })} /> Test users
        </label>
        <button type="button" className="dw-linkish" data-testid="np-talks-clear" onClick={() => { onChange(null); setOpen(false); }}>Remove</button>
      </div>
      {problem ? <span className="dw-field-hint" role="status">{problem}</span>
        : value?.provider ? <span className="dw-field-hint">It reads <code>BOSS_LINK_{value.alias}_URL</code>{value.testUsers ? <> and <code>BOSS_LINK_{value.alias}_USERS</code></> : null}.</span> : null}
    </div>
  );
}
