"use client";

/**
 * Try it (spec §5.3 5): one read-only call through BOSS's receipted path, as BOSS — proof the
 * tool works end to end, not only that the server answers. Only for an ability with a tier-0
 * tool set to Allow (the API refuses anything else).
 */
import React, { useState } from "react";

import type { Ability } from "../../lib/kit-types";
import { kitApi } from "../../lib/kit-api";
import { shortError } from "./kit-copy";

export function TrySection({ ability, projectId, actor }: { ability: Ability; projectId: string; actor: string }) {
  const field = ability.try?.field;
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ ok: boolean; preview: string } | null>(null);
  if (!field) return null;
  const run = async () => {
    setBusy(true); setOut(null);
    const r = await kitApi.tryIt(projectId, actor, ability.key, value);
    setBusy(false);
    setOut(r.ok ? { ok: r.data.ok, preview: r.data.preview } : { ok: false, preview: r.error });
  };
  return (
    <div className="kit-try" data-testid="kit-try">
      <label className="kit-field">
        <span className="kit-field-label">{field.label}</span>
        <span className="kit-inline">
          <input type="text" value={value} placeholder={field.placeholder ?? ""} spellCheck={false}
                 data-testid="kit-try-input" onChange={(e) => setValue(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && value.trim()) void run(); }} />
          <button type="button" className="kit-btn" disabled={busy || !value.trim()} data-testid="kit-try-run"
                  onClick={() => void run()}>{busy ? "Testing…" : "Run"}</button>
        </span>
      </label>
      {out ? (
        out.ok
          ? <pre className="kit-try-out" data-testid="kit-try-out" data-user-text>{out.preview}</pre>
          : <div className="kit-err" role="alert" data-tip={out.preview} data-testid="kit-try-error">{shortError(out.preview)}</div>
      ) : null}
    </div>
  );
}
