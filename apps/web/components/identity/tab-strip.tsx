/** The Identity (or My account) tab strip: only openable pages, the current one marked. */
import React from "react";

export function TabStrip({ tabs, current, label }: {
  tabs: { path: string; label: string; tip: string; count: number | null }[];
  current: string;
  label: string;
}) {
  // The current tab is the longest tab path the current path starts with (/identity/people/x → People).
  const here = tabs.map((t) => t.path).filter((p) => current === p || current.startsWith(`${p}/`))
    .sort((a, b) => b.length - a.length)[0];
  return (
    <nav className="idn-tabs" aria-label={label} data-testid="identity-tabs">
      {tabs.map((t) => (
        <a key={t.path} href={t.path} title={t.tip} aria-current={t.path === here ? "page" : undefined} data-tab={t.path}>
          {t.label}
          {t.count !== null ? <span className="idn-n">{t.count}</span> : null}
        </a>
      ))}
    </nav>
  );
}
