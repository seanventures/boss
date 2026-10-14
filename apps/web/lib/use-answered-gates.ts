"use client";

import { useEffect, useState } from "react";

import { DECIDED_EVENT } from "./decided-gates";

/** Re-render when this console answers a decision (lib/decided-gates.ts): the version moves on
 *  every answer. Kept out of decided-gates.ts, which lib/api-write.ts imports on the server too. */
export function useAnsweredGates(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const bump = () => setVersion((v) => v + 1);
    window.addEventListener(DECIDED_EVENT, bump);
    return () => window.removeEventListener(DECIDED_EVENT, bump);
  }, []);
  return version;
}
