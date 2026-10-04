/**
 * The console's Live / Paused chip, as a value any panel can read (agents-home spec §4.7, §5.2).
 *
 * ConsoleShell owns the chip and publishes it here. Agents › Live and the presence pill stop
 * polling while it says Paused, so "Paused" means nothing on the page updates or moves — and
 * pressing Live again catches up at once.
 */
import { useEffect, useState } from "react";

let live = true;
const listeners = new Set<(live: boolean) => void>();

export function isConsoleLive(): boolean {
  return live;
}

export function setConsoleLive(next: boolean): void {
  if (next === live) return;
  live = next;
  for (const l of [...listeners]) l(live);
}

export function subscribeConsoleLive(listener: (live: boolean) => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useConsoleLive(): boolean {
  const [value, setValue] = useState(live);
  useEffect(() => {
    setValue(live);
    return subscribeConsoleLive(setValue);
  }, []);
  return value;
}
