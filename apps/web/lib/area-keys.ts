/**
 * AN AREA'S KEYBOARD SHORTCUTS WORK ONLY WHILE THAT AREA IS ON SCREEN (KG tester round 1, P0-5).
 *
 * Every area the session opened stays mounted and the CSS reveal only hides it, so a shortcut an
 * area registered on `window` kept firing from every other area: the Activity log's j/k/↓/↑
 * stepped its hidden list (and pushed its URL) while a person arrowed through the Graph's tree.
 * A shortcut — any key other than closing an open overlay — is registered through this hook,
 * which listens only while the area holding it is shown (app/area-frame.tsx) and the tab is
 * visible. tests/every-area-shortcut-is-scoped-to-its-area.test.ts fails on a raw window/document
 * keydown listener that handles anything but Escape/Tab.
 */
import { useEffect, useRef } from "react";

import { useOnScreen } from "./area-shown";

export function useAreaKeydown(
  handler: (e: KeyboardEvent) => void,
  opts: { target?: "window" | "document"; capture?: boolean; enabled?: boolean } = {},
): void {
  const shown = useOnScreen();
  const ref = useRef(handler);
  ref.current = handler;
  const { target = "window", capture = false, enabled = true } = opts;
  useEffect(() => {
    if (!shown || !enabled || typeof window === "undefined") return undefined;
    const on = (e: KeyboardEvent) => ref.current(e);
    const t: Window | Document = target === "document" ? document : window;
    t.addEventListener("keydown", on as EventListener, capture);
    return () => t.removeEventListener("keydown", on as EventListener, capture);
  }, [shown, enabled, target, capture]);
}
