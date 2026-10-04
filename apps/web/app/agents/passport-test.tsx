"use client";

/**
 * Profile › Test, made real (spec §3.4 › Profile › Test).
 *
 * Three probes, each a real model call through the agent's own compiled prompt, streamed into the
 * page while its face speaks: introduce yourself, push back on a plan, and refuse something out
 * of scope. A cheap judge then scores it — `Voice 4/5 · Stays in remit ✓ · Says it's AI ✓` — so
 * a voice edit is heard and checked before it reaches real work. An unsaved draft can be tested:
 * the editor passes it in, and the API hears it without saving it. With no model configured it
 * says so and where to fix it, rather than failing silently.
 */
import React, { useEffect, useRef, useState } from "react";
import { Flask } from "@phosphor-icons/react";

import type { AvatarState, Persona } from "../../lib/agent-world-types";
import { identityOf, runPersonaTest, type Probe, type TestResult } from "../../lib/agent-world";
import { AgFace } from "./agent-face";

export const PROBES: { key: Probe; label: string }[] = [
  { key: "intro", label: "Introduce yourself" },
  { key: "push_back", label: "Push back on this" },
  { key: "out_of_scope", label: "Out of scope" },
];

/** `Voice 4/5 · Stays in remit ✓ · Says it's AI ✓` */
export function resultLine(r: TestResult): string {
  return [`Voice ${r.voice}/5`, `Stays in remit ${r.inRemit ? "✓" : "✗"}`, `Says it's AI ${r.saysAi ? "✓" : "✗"}`].join(" · ");
}

export function PassportTest({ projectId, actor, roleKey, persona }: {
  projectId: string; actor: string; roleKey: string;
  /** An unsaved voice to hear instead of the saved one. */
  persona?: Persona | null;
}) {
  const [probe, setProbe] = useState<Probe | null>(null);
  const [text, setText] = useState("");
  const [state, setState] = useState<AvatarState>("idle");
  const [result, setResult] = useState<TestResult | null>(null);
  const [failed, setFailed] = useState("");
  const abort = useRef<AbortController | null>(null);
  const quiet = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { abort.current?.abort(); if (quiet.current) clearTimeout(quiet.current); }, []);

  const run = async (p: Probe) => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setProbe(p); setText(""); setResult(null); setFailed(""); setState("thinking");
    try {
      for await (const ev of runPersonaTest(projectId, actor, roleKey, p, persona ?? undefined, ctl.signal)) {
        if (ctl.signal.aborted) return;
        if (ev.type === "token") {
          setText((t) => t + ev.text);
          // Speaking while text arrives; back to still 300ms after the last chunk (spec §7).
          setState("speaking");
          if (quiet.current) clearTimeout(quiet.current);
          quiet.current = setTimeout(() => setState((s) => (s === "speaking" ? "thinking" : s)), 300);
        } else if (ev.type === "result") {
          setResult(ev.result);
          setState(ev.result.inRemit && ev.result.saysAi ? "done" : "stuck");
        } else {
          setFailed(ev.code === "needs_model" ? "Needs a model. Set one in Settings." : ev.message);
          setState("stuck");
        }
      }
      setState((s) => (s === "thinking" || s === "speaking" ? "idle" : s));
    } catch (e) {
      if (!ctl.signal.aborted) { setFailed(String((e as Error)?.message ?? e)); setState("stuck"); }
    }
  };

  const busy = state === "thinking" || state === "speaking";
  const who = identityOf(roleKey);
  return (
    <div className="ag-test" data-testid="ag-test">
      <div className="ag-test-head">
        <span className="ag-field-label" title="Real model call with this exact prompt.">
          <Flask size={13} aria-hidden /> Test{persona ? " · draft" : ""}
        </span>
        {PROBES.map((x) => (
          <button key={x.key} type="button" className="ag-btn" disabled={busy} aria-pressed={probe === x.key}
                  data-testid={`ag-probe-${x.key}`} onClick={() => void run(x.key)}>{x.label}</button>
        ))}
      </div>
      {probe ? (
        <div className="ag-test-out" data-state={state} aria-busy={busy}>
          <AgFace roleKey={roleKey} identity={who} size={32} state={state} />
          <div className="ag-test-text">
            {text ? <span className="ag-test-reply" data-testid="ag-test-reply" data-user-text>{text}</span>
              : busy ? <span className="ag-muted">Thinking…</span> : null}
            {result ? (
              <span className="ag-test-result" data-testid="ag-test-result"
                    data-pass={result.inRemit && result.saysAi}>{resultLine(result)}</span>
            ) : null}
            {failed ? <span className="ag-bad" role="alert" data-testid="ag-test-failed">{failed}</span> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
