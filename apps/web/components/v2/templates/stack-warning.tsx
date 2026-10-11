"use client";

/**
 * CAN BOSS BUILD WHAT THIS ASKS FOR? Asked while the person types (GET /v1/stacks, debounced),
 * shown before Start: "This brief asks for Go, which BOSS cannot build. BOSS builds Python
 * (FastAPI, …) …". The API refuses the same start with `stack_unsupported` unless the person ticks
 * "Start anyway", which sends `allow_unsupported_stack` — the brief is then kept as a plan, and
 * the build refuses it again before anything is spent.
 */
import React, { useEffect, useState } from "react";

import { checkStack, type StackCheck } from "../../../lib/templates";

export function useStackCheck(projectId: string, text: string, stack?: string) {
  const [check, setCheck] = useState<StackCheck | null>(null);
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    let alive = true;
    const t = window.setTimeout(() => {
      void checkStack(projectId, text, stack).then((c) => { if (alive) setCheck(c); });
    }, 500);
    return () => { alive = false; window.clearTimeout(t); };
  }, [projectId, text, stack]);
  useEffect(() => { setAllowed(false); }, [check?.message]);
  const refused = !!check && !check.ok;
  return { check, refused, allowed, setAllowed, blocks: refused && !allowed };
}

export function StackWarning({ state, testId = "stack-warning" }: {
  state: ReturnType<typeof useStackCheck>; testId?: string;
}) {
  if (!state.refused || !state.check) return null;
  return (
    <div className="tpl-warn" role="status" data-testid={testId}>
      <p>{state.check.message}</p>
      <label title="It starts, and the panel and documents run; the build refuses it before anything is spent.">
        <input type="checkbox" checked={state.allowed} data-testid={`${testId}-anyway`}
               onChange={(e) => state.setAllowed(e.target.checked)} />{" "}
        Start anyway
      </label>
    </div>
  );
}
