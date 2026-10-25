"use client";

import { useCallback, useEffect, useState, type RefObject, type SyntheticEvent } from "react";

/**
 * A <details> MENU CLOSES WHEN THE PERSON LOOKS AWAY: on Esc (focus back on its summary) and on a
 * press anywhere outside it (KG tester rounds 2 and 5: the project menu, then the System menu,
 * stayed open over the page). One hook for every header menu built on <details>.
 *
 *   const ref = useRef<HTMLDetailsElement>(null);
 *   const { onToggle, close } = useDetailsDismiss(ref);
 *   <details ref={ref} onToggle={onToggle}>…</details>
 */
export function useDetailsDismiss(ref: RefObject<HTMLDetailsElement | null>): {
  open: boolean; onToggle: (e: SyntheticEvent<HTMLDetailsElement>) => void; close: () => void;
} {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => { if (ref.current) ref.current.open = false; setOpen(false); }, [ref]);
  const onToggle = useCallback((e: SyntheticEvent<HTMLDetailsElement>) => setOpen(e.currentTarget.open), []);
  useEffect(() => {
    if (!open) return undefined;
    const outside = (e: PointerEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    const escape = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      close();
      ref.current?.querySelector<HTMLElement>("summary")?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open, close, ref]);
  return { open, onToggle, close };
}
