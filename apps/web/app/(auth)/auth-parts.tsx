"use client";

/**
 * The sign-in sheet's frame and parts (mockup e, D33): the BOSS mark and host, the provider icon
 * row, the method tabs, the "or" divider. Shared by sign-in, claim, invitation, enrolment,
 * recovery and the email landing pages so every auth screen is the same panel.
 */
import React, { useRef, type ReactNode } from "react";
import { AppleLogo, GithubLogo, GoogleLogo, Key, WindowsLogo } from "@phosphor-icons/react";

import type { AuthOptions } from "../../lib/identity/types";
import { AUTH_LINKS, type AuthLinkKind } from "../../lib/identity/auth-links.gen";

export function AuthStage({ children, wide = false, label }: { children: ReactNode; wide?: boolean; label: string }) {
  return (
    <main className="idn-auth">
      <section className={`idn-panel${wide ? " wide" : ""}`} aria-label={label}>
        {children}
      </section>
    </main>
  );
}

export function Logo({ host, right }: { host?: string | null; right?: ReactNode }) {
  return (
    <div className="idn-logo">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/boss-mark.png" alt="" width={28} height={28} />
      <b>BOSS</b>
      <span>{right ?? host ?? ""}</span>
    </div>
  );
}

const ICONS: Partial<Record<AuthOptions["providers"][number]["preset"], ReactNode>> = {
  google: <GoogleLogo size={18} weight="bold" aria-hidden />,
  github: <GithubLogo size={18} weight="fill" aria-hidden />,
  microsoft: <WindowsLogo size={18} weight="fill" aria-hidden />,
  apple: <AppleLogo size={18} weight="fill" aria-hidden />,
};

/**
 * One button per provider the Owner configured; nothing at all when none is (§9.7). Social
 * presets are icons (named for screen readers), enterprise providers say their name, and a lone
 * provider becomes a full-width "Continue with X".
 */
export function ProviderRow({ providers, next }: { providers: AuthOptions["providers"]; next: string }) {
  if (!providers.length) return null;
  const href = (id: string) => `/v1/auth/sso/${encodeURIComponent(id)}/start?next=${encodeURIComponent(next)}`;
  if (providers.length === 1) {
    const p = providers[0];
    return (
      <div className="idn-provs">
        <a className="idn-pv full" href={href(p.id)} data-testid={`sso-${p.id}`}>{ICONS[p.preset] ?? <Key size={16} aria-hidden />} Continue with {p.name}</a>
      </div>
    );
  }
  const social = providers.filter((p) => ICONS[p.preset]);
  const enterprise = providers.filter((p) => !ICONS[p.preset]);
  return (
    <div className="idn-provs" role="group" aria-label="Sign in with">
      {social.map((p) => (
        <a key={p.id} className="idn-pv icon" href={href(p.id)} aria-label={`Continue with ${p.name}`} title={p.name} data-testid={`sso-${p.id}`}>{ICONS[p.preset]}</a>
      ))}
      {enterprise.map((p) => (
        <a key={p.id} className="idn-pv" href={href(p.id)} data-testid={`sso-${p.id}`}><Key size={15} aria-hidden /> {p.name}</a>
      ))}
    </div>
  );
}

/** A tablist that behaves like one: arrow keys move, Home/End jump, only the active tab is in the Tab order. */
export function MethodTabs<T extends string>({ tabs, value, onChange, label }: {
  tabs: { id: T; label: string }[]; value: T; onChange: (id: T) => void; label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  if (tabs.length < 2) return null;
  const move = (i: number) => {
    const j = (i + tabs.length) % tabs.length;
    onChange(tabs[j].id);
    refs.current[j]?.focus();
  };
  return (
    <div className="idn-mtabs" role="tablist" aria-label={label}>
      {tabs.map((t, i) => (
        <button key={t.id} ref={(el) => { refs.current[i] = el; }} type="button" role="tab" id={`tab-${t.id}`}
          aria-selected={value === t.id} aria-controls={`panel-${t.id}`} tabIndex={value === t.id ? 0 : -1}
          onClick={() => onChange(t.id)}
          onKeyDown={(e) => {
            if (e.key === "ArrowRight") { e.preventDefault(); move(i + 1); }
            if (e.key === "ArrowLeft") { e.preventDefault(); move(i - 1); }
            if (e.key === "Home") { e.preventDefault(); move(0); }
            if (e.key === "End") { e.preventDefault(); move(tabs.length - 1); }
          }}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** The one polite live region an auth screen announces into (errors use role=alert). */
export function Live({ text }: { text: string }) {
  return <div className="idn-sr" role="status" aria-live="polite">{text}</div>;
}

/** A redirect target that stays on this console (no protocol-relative, no backslash tricks). */
export function safeNextClient(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\") || /[\u0000-\u001f\u007f]/.test(value)) return "/";
  if (/^\/(login|claim|invite|signin|reset|signup|enrol)(\/|$|\?)/.test(value)) return "/";
  return value;
}

/**
 * The token of a link the engine minted, read from the fragment key the ONE table names
 * (lib/identity/auth-links.gen.ts, from engine/boss_engine/identity/links.py). Links carry their
 * token after #, so it never reaches an access log. Every page a link lands on reads it this way;
 * tests/every-minted-link-lands-on-a-real-page.test.ts holds them to it.
 */
export function linkToken(kind: AuthLinkKind): string {
  if (typeof window === "undefined") return "";
  const key = AUTH_LINKS[kind].fragment;
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const query = new URLSearchParams(window.location.search);
  return hash.get(key) ?? query.get(key) ?? "";
}

/** The fragment's `t`: the magic-link page's older spelling of `linkToken("magic_link")`. */
export function fragmentToken(): string {
  return linkToken("magic_link");
}
