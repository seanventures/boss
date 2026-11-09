"use client";

import { useEffect, useState } from "react";
import {
  formatAgeCompact,
  formatDateTime,
  formatRelativeTime,
} from "../lib/datetime";

type Style = "long" | "short" | "compact";

/**
 * Renders a stable absolute UTC timestamp on SSR and the client's first paint,
 * then upgrades to a relative label after mount. That keeps React hydration
 * matched (no #418) while still giving operators a live "3m ago" feel.
 */
export function ClientRelativeTime({
  iso,
  style="long",
  empty = "not yet",
  prefix = "",
  suffix = "",
  className,
  as: Tag = "time",
}: {
  iso?: string | null;
  style?: Style;
  empty?: string;
  prefix?: string;
  suffix?: string;
  className?: string;
  as?: "time" | "span" | "small";
}) {
  const absolute = iso ? formatDateTime(iso, empty) : empty;
  const [label, setLabel] = useState(absolute);

  useEffect(() => {
    if (!iso) {
      setLabel(empty);
      return;
    }
    const tick = () => {
      const now = Date.now();
      if (style === "compact") {
        const age = formatAgeCompact(iso, now);
        setLabel(age ? `${age}${suffix || " ago"}` : empty);
      } else {
        setLabel(formatRelativeTime(iso, now, { empty, style }));
      }
    };
    tick();
    const id = window.setInterval(tick, 30_000);
    return () => window.clearInterval(id);
  }, [iso, empty, style, suffix]);

  const body = `${prefix}${label}`;
  if (Tag === "time") {
    return (
      <time className={className} dateTime={iso ?? undefined}>
        {body}
      </time>
    );
  }
  if (Tag === "small") {
    return <small className={className}>{body}</small>;
  }
  return <span className={className}>{body}</span>;
}
