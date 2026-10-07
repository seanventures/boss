"use client";

/**
 * The auth screens' frame and parts: the BOSS mark, the provider squares, the method switcher,
 * the tooltip. Shared by sign-in, sign-up, claim, invitation, enrolment, reset and the email
 * landing pages so every auth screen is one panel in one visual family.
 */
import React, { useId, useRef, type ReactNode } from "react";
import { AppleLogo, GithubLogo, Globe } from "@phosphor-icons/react";

import type { ProviderOffer } from "../../lib/identity/sign-in-offers";

export function AuthStage({ children, wide = false, label }: { children: ReactNode; wide?: boolean; label: string }) {
  return (
    <main className="idn-auth">
      <section className={`idn-panel${wide ? " wide" : ""}`} aria-label={label}>
        {children}
      </section>
    </main>
  );
}

/**
 * The large centred BOSS mark and wordmark. `right` is a short fact about this screen ("expires
 * in 3 days"), shown as a tag under the mark; `host` sits quietly at the foot of the panel.
 */
export function Logo({ host, right }: { host?: string | null; right?: ReactNode }) {
  return (
    <>
      <div className="idn-brand">
        <span className="idn-brand-mark" aria-hidden>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/boss-mark.png" alt="" width={64} height={68} />
        </span>
        <b className="idn-brand-word">BOSS</b>
        {right && right !== host ? <span className="idn-brand-tag" data-copy="data">{right}</span> : null}
      </div>
      {host ? <HostLine host={host} /> : null}
    </>
  );
}

/** Which BOSS this is, at the foot of the panel. */
export function HostLine({ host, children }: { host: string; children?: ReactNode }) {
  return (
    <div className={`idn-hostline${children ? " with" : ""}`}>
      <span className="idn-host" data-copy="data"><Globe size={13} aria-hidden /> {host}</span>
      {children}
    </div>
  );
}

/**
 * A hover-and-focus tooltip (role="tooltip", tied by aria-describedby). Used for every disabled
 * option, so a keyboard or screen-reader user hears the same reason a mouse user sees.
 */
export function Tip({ text, children, className = "" }: { text: string; children: (describedBy: string) => ReactNode; className?: string }) {
  const id = useId();
  return (
    <span className={`idn-tipw ${className}`.trim()} role="presentation">
      {children(id)}
      <span className="idn-tip" role="tooltip" id={id}>{text}</span>
    </span>
  );
}

/** The provider's own mark: brand colours for Google and Microsoft, ink for GitHub and Apple, initials for the rest. */
export function ProviderIcon({ preset, name }: { preset: ProviderOffer["preset"]; name: string }) {
  if (preset === "google") {
    return (
      <svg className="idn-gg" viewBox="0 0 48 48" width={20} height={20} aria-hidden focusable="false">
        <path className="r" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
        <path className="b" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
        <path className="y" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
        <path className="g" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
      </svg>
    );
  }
  if (preset === "microsoft") {
    return (
      <svg className="idn-ms" viewBox="0 0 21 21" width={19} height={19} aria-hidden focusable="false">
        <rect className="r" x="1" y="1" width="9" height="9" /><rect className="g" x="11" y="1" width="9" height="9" />
        <rect className="b" x="1" y="11" width="9" height="9" /><rect className="y" x="11" y="11" width="9" height="9" />
      </svg>
    );
  }
  if (preset === "github") return <GithubLogo size={21} weight="fill" aria-hidden />;
  if (preset === "apple") return <AppleLogo size={21} weight="fill" aria-hidden />;
  const letters = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase() ?? "").join("") || "?";
  return <span className="idn-mono" aria-hidden>{letters}</span>;
}

/**
 * The provider squares: Google, GitHub, Microsoft and Apple always (live when configured), then
 * every other configured provider. A square that isn't set up says why on hover and focus, and
 * again in words on a tap (`onWhy`), since a phone has no hover.
 */
export function ProviderRow({ offers, onWhy }: { offers: ProviderOffer[]; onWhy: (why: string) => void }) {
  return (
    <div className={`idn-idps n${Math.min(offers.length, 6)}`} role="group" aria-label="Sign in with">
      {offers.map((o) => (
        <Tip key={o.key} text={o.why ?? `Continue with ${o.name}`}>
          {(tip) => o.href ? (
            <a className="idn-idp" href={o.href} aria-label={`Continue with ${o.name}`} aria-describedby={tip} data-testid={`sso-${o.key}`}>
              <ProviderIcon preset={o.preset} name={o.name} />
            </a>
          ) : (
            <button type="button" className="idn-idp" aria-disabled="true" aria-label={`${o.name}, not set up`} aria-describedby={tip}
              data-testid={`sso-${o.key}`} onClick={() => onWhy(o.why ?? "")}>
              <ProviderIcon preset={o.preset} name={o.name} />
            </button>
          )}
        </Tip>
      ))}
    </div>
  );
}

/**
 * A segmented tablist that behaves like one: arrow keys move between the methods,
 * Home/End jump, only the active tab is in the Tab order. A method that's off stays visible,
 * is reached by the arrows but never chosen, says why on hover and focus, and in words when
 * pressed (`onWhy`).
 */
export function MethodTabs<T extends string>({ tabs, value, onChange, label, onWhy }: {
  tabs: { id: T; label: string; icon?: ReactNode; why?: string | null }[]; value: T; onChange: (id: T) => void; label: string;
  onWhy?: (why: string) => void;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  if (tabs.length < 2) return null;
  // Arrows walk every method, the ones that are off included (focus shows why), but only a live one is chosen.
  const move = (j: number) => {
    const k = ((j % tabs.length) + tabs.length) % tabs.length;
    if (!tabs[k].why) onChange(tabs[k].id);
    refs.current[k]?.focus();
  };
  return (
    <div className="idn-mtabs seg" role="tablist" aria-label={label}>
      {tabs.map((t, i) => {
        const tab = (tip?: string) => (
          <button ref={(el) => { refs.current[i] = el; }} type="button" role="tab" id={`tab-${t.id}`}
            aria-selected={value === t.id} aria-controls={t.why ? undefined : `panel-${t.id}`} aria-disabled={t.why ? true : undefined}
            aria-describedby={tip} tabIndex={value === t.id ? 0 : -1}
            onClick={() => (t.why ? onWhy?.(t.why) : onChange(t.id))}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") { e.preventDefault(); move(i + 1); }
              if (e.key === "ArrowLeft") { e.preventDefault(); move(i - 1); }
              if (e.key === "Home") { e.preventDefault(); move(0); }
              if (e.key === "End") { e.preventDefault(); move(tabs.length - 1); }
            }}>
            {t.icon}{t.label}
          </button>
        );
        return t.why ? <Tip key={t.id} text={t.why} className="seg">{(tip) => tab(tip)}</Tip> : <React.Fragment key={t.id}>{tab()}</React.Fragment>;
      })}
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

/** The fragment's `t` (links carry their token after #, so it never reaches an access log). */
export function fragmentToken(): string {
  if (typeof window === "undefined") return "";
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const query = new URLSearchParams(window.location.search);
  return hash.get("t") ?? query.get("t") ?? "";
}
