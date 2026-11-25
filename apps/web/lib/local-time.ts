/**
 * THE VIEWER'S CLOCK, ONLY AFTER MOUNT. Infra and Observability show times in the viewer's own
 * zone and locale (`14:15`, `Oct 4`). The server renders in ITS zone, so a time painted during
 * the server render and again on the client disagrees, and React 19 keeps the server's text — the
 * datetime-hydration guard forbids it. `useLocalTime()` hands a component formatters that answer
 * "" until it has mounted in the browser, then the real local text; the first client render
 * matches the server's exactly.
 */
import { useEffect, useState } from "react";

import { LOCAL, NOT_YET, markHydrated, type LocalTime } from "./hydration-state";

export {
  LOCAL,
  NOT_YET,
  isHydrated,
  localFull,
  localWith,
  markHydrated,
  sinceLocal,
  stampLocal,
  viewerZone,
  whenLocal,
  type LocalTime,
} from "./hydration-state";

export function useLocalTime(): LocalTime {
  const [ready, setReady] = useState(false);
  useEffect(() => { markHydrated(); setReady(true); }, []);
  return ready ? LOCAL : NOT_YET;
}
