"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Switch between the current console and v2.
 *
 * This exists so v2 can be built in the open without stranding anyone: v1 keeps taking bug
 * fixes and stays the working console until v2 is genuinely complete, and the same operator
 * can compare both against the same live data.
 *
 * The choice is remembered, because a preference that resets on every navigation is not a
 * preference. `?console=v1|v2` overrides it for a single visit — useful for a link in a bug
 * report that must land on a specific console regardless of what the reader chose before.
 */

const KEY = "boss.console";

export function ConsoleToggle({
  current,
  orgId,
}: {
  current: "v1" | "v2";
  orgId?: string;
}) {
  const [pending, setPending] = useState(false);

  // Record the console the operator is actually looking at, so a plain visit to "/" next time
  // lands where they left off.
  useEffect(() => {
    try {
      window.localStorage.setItem(KEY, current);
    } catch {
      /* private mode: the toggle still works, it just will not be remembered */
    }
  }, [current]);

  const go = useCallback(
    (target: "v1" | "v2") => {
      if (target === current) return;
      setPending(true);
      try {
        window.localStorage.setItem(KEY, target);
      } catch {
        /* ignore */
      }
      const org = orgId ? `org=${encodeURIComponent(orgId)}` : "";
      // A full navigation, deliberately: the two consoles are different React trees, and a
      // client-side transition between them would keep v1's state mounted underneath.
      window.location.assign(target === "v2" ? `/v2?${org}` : `/?${org}`);
    },
    [current, orgId],
  );

  return (
    <div className="v2-toggle" role="group" aria-label="Console version" data-testid="console-toggle">
      <button
        type="button"
        className="v2-toggle-btn"
        data-active={current === "v1"}
        disabled={pending}
        onClick={() => go("v1")}
        title="The current console — every feature, still taking fixes"
      >
        v1
      </button>
      <button
        type="button"
        className="v2-toggle-btn"
        data-active={current === "v2"}
        disabled={pending}
        onClick={() => go("v2")}
        title="The new console — in progress"
      >
        v2
      </button>
    </div>
  );
}
