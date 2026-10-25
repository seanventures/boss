"use client";

/**
 * The identity screens' small parts, one definition each: faces, role and permission chips,
 * pills, relative time, the modal sheet and the six-box code entry. Classes are identity.css's
 * idn- tokens; no inline style anywhere (tests/identity-is-tokens-only.test.ts).
 */
import React, { createContext, useContext, useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { PERMISSION_META, type Permission } from "../../lib/identity/permissions.gen";
import { deltaParts, permissionName, permissionTip } from "../../lib/identity/words";

// ---- time: the server's clock, so the first paint and hydration say the same thing ----------

const NowCtx = createContext<number>(0);
export function NowProvider({ now, children }: { now: number; children: ReactNode }) {
  return <NowCtx.Provider value={now}>{children}</NowCtx.Provider>;
}
export function useNow(): number {
  const n = useContext(NowCtx);
  return n || Date.now();
}

export function agoText(iso: string | null | undefined, now: number, empty = "never"): string {
  if (!iso) return empty;
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return empty;
  const d = now - t;
  const future = d < 0;
  const m = Math.floor(Math.abs(d) / 60_000);
  if (m < 1) return "now";
  const s = m < 60 ? `${m}m` : m < 60 * 36 ? `${Math.round(m / 60)}h` : `${Math.round(m / 1440)}d`;
  return future ? `in ${s}` : `${s} ago`;
}

/** "3m ago", "in 6 days" — from the page's clock, stable through hydration. */
export function Ago({ iso, empty = "never", bare = false }: { iso: string | null | undefined; empty?: string; bare?: boolean }) {
  const now = useNow();
  const text = agoText(iso, now, empty);
  return <time dateTime={iso ?? undefined} title={iso ? iso.replace("T", " ").slice(0, 16) + " UTC" : undefined}>{bare ? text.replace(/ ago$/, "") : text}</time>;
}

export function daysUntil(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  return Math.ceil((Date.parse(iso) - now) / 86_400_000);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "Nov 5" in UTC: never locale- or timezone-dependent (hydration-safe). */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}
/** "10:41:03" in UTC. */
export function clock(iso: string): string {
  return new Date(iso).toISOString().slice(11, 19);
}

// ---- people and agents ----------------------------------------------------------------------

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return ((parts[0][0] ?? "") + (parts.length > 1 ? parts[parts.length - 1][0] : parts[0][1] ?? "")).toUpperCase();
}

export function Face({ name, tone, size = 30, label = false }: { name: string; tone: number; size?: 22 | 26 | 30 | 34 | 44 | 78; label?: boolean }) {
  return (
    <span className="idn-face" data-tone={String(((tone % 8) + 8) % 8)} data-size={size} aria-hidden={label ? undefined : true} role={label ? "img" : undefined} aria-label={label ? name : undefined}>
      {initials(name)}
    </span>
  );
}

// ---- chips ----------------------------------------------------------------------------------

const BUILTIN = new Set(["owner", "admin", "operator", "viewer", "auditor"]);
export function roleTone(key: string): string {
  return BUILTIN.has(key) ? key : "custom";
}

export function RoleChip({ roleKey, name, scope }: { roleKey: string; name: string; scope?: string | null }) {
  const where = scope ? (scope === "org" ? "org" : scope.replace(/^project:/, "")) : null;
  return (
    <span className="idn-role" data-role-tone={roleTone(roleKey)}>
      {name}{where ? <i>{where}</i> : null}
    </span>
  );
}

/**
 * A permission, as people read it: its NAME ("Invite people"); the key (user:invite) is only the
 * tooltip. The one renderer of a permission on every identity screen (lib/identity/words.ts).
 */
export function Perm({ p, state, sm = false }: { p: Permission | string; state?: "off" | "ghost"; sm?: boolean }) {
  const meta = (PERMISSION_META as Record<string, (typeof PERMISSION_META)[Permission] | undefined>)[p];
  const cls = ["idn-perm", sm ? "sm" : "", state ?? (meta?.critical ? "crit" : "")].filter(Boolean).join(" ");
  return <span className={cls} title={permissionTip(p)} data-permission={p}>{permissionName(p)}</span>;
}

/** A count's change: "+3 −2", one side when the other is zero, "no change" when both are. */
export function Delta({ gained, lost }: { gained: number; lost: number }) {
  const d = deltaParts(gained, lost);
  if (d.none) return <span className="idn-dim" title="Nothing gained or lost">no change</span>;
  return (
    <>
      {d.gained ? <span className="idn-plus">{d.gained}</span> : null}
      {d.gained && d.lost ? " " : null}
      {d.lost ? <span className="idn-minus">{d.lost}</span> : null}
    </>
  );
}

export function Pill({ tone, children, tip }: { tone: "ok" | "warn" | "bad" | "info" | "mute"; children: ReactNode; tip?: string }) {
  return <span className={`idn-pill ${tone}`} title={tip}>{children}</span>;
}

// ---- the modal sheet ------------------------------------------------------------------------

/**
 * A dialog: aria-modal, labelled by its heading, Escape closes (unless `locked`), focus moves in
 * on open, stays inside while open, and returns to the opener on close.
 */
/** A dialog for one job: centred, or `side` (docked to the right, full height: a list's row
 * opens it and the list stays in view beside it). */
export function Sheet({ title, icon, onClose, wide = false, side = false, locked = false, children, testId, layer }: {
  title: ReactNode; icon?: ReactNode; onClose: () => void; wide?: boolean; side?: boolean; locked?: boolean; children: ReactNode; testId?: string;
  /** "step-up": the top of the console's z-order scale (--z-step-up), at the end of <body>, so it
   *  is above whatever sheet or dialog is waiting on it — never inside the shell's stacking context. */
  layer?: "step-up";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // FOCUS ONCE, ON OPEN. The effect depended on [onClose, locked]: a parent that passes a new
  // inline onClose each render (and IdentityProvider re-checks identity every 30 s and on window
  // focus) re-ran it, which focused the first control again and scrolled the sheet back to the
  // top while the person was scrolling or typing (operator, Let an agent act for me). The latest
  // onClose/locked are read through refs instead.
  const closeRef = useRef(onClose);
  const lockedRef = useRef(locked);
  closeRef.current = onClose;
  lockedRef.current = locked;
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = ref.current;
    const first = root?.querySelector<HTMLElement>("[data-autofocus], input, button, [href], textarea, select");
    first?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !lockedRef.current) { e.preventDefault(); closeRef.current(); }
      if (e.key === "Tab" && root) {
        const items = [...root.querySelectorAll<HTMLElement>("input, button, [href], textarea, select, [tabindex]:not([tabindex='-1'])")]
          .filter((el) => !el.hasAttribute("disabled"));
        if (!items.length) return;
        const a = items[0], z = items[items.length - 1];
        if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
        else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); opener?.focus?.({ preventScroll: true }); };
  }, []);
  const wrap = (
    <div className={`idn-sheet-wrap${side ? " side" : ""}`} data-layer={layer} onMouseDown={(e) => { if (e.target === e.currentTarget && !locked) onClose(); }}>
      <div ref={ref} className={`idn-sheet${wide ? " wide" : ""}${side ? " side" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid={testId}>
        <h2 id={titleId}>{icon}{title}</h2>
        {children}
      </div>
    </div>
  );
  return layer && typeof document !== "undefined" ? createPortal(wrap, document.body) : wrap;
}

// ---- six-digit code entry --------------------------------------------------------------------

/**
 * Six boxes that behave like one field: typing advances, Backspace goes back, a pasted code
 * fills every box, and the whole code is submitted the moment the sixth digit lands. One
 * accessible name for the group; each box says which digit it is.
 */
export function CodeInput({ value, onChange, onComplete, label, size, invalid = false, autoFocus = true }: {
  value: string; onChange: (v: string) => void; onComplete?: (v: string) => void; label: string;
  size?: "sm"; invalid?: boolean; autoFocus?: boolean;
}) {
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const digits = value.padEnd(6, " ").slice(0, 6).split("");
  useEffect(() => { if (autoFocus) refs.current[Math.min(value.length, 5)]?.focus(); }, [autoFocus]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (next: string) => {
    const clean = next.replace(/\D/g, "").slice(0, 6);
    onChange(clean);
    if (clean.length === 6) onComplete?.(clean);
    refs.current[Math.min(clean.length, 5)]?.focus();
  };
  return (
    <div className={`idn-otp${size ? ` ${size}` : ""}`} role="group" aria-label={label}>
      {digits.map((d, i) => (
        <React.Fragment key={i}>
          {i === 3 ? <span className="idn-gap" aria-hidden /> : null}
          <input
            ref={(el) => { refs.current[i] = el; }}
            inputMode="numeric"
            autoComplete={i === 0 ? "one-time-code" : "off"}
            pattern="[0-9]*"
            maxLength={6}
            aria-label={`Digit ${i + 1} of 6`}
            aria-invalid={invalid || undefined}
            value={d.trim()}
            onChange={(e) => {
              const typed = e.target.value.replace(/\D/g, "");
              if (typed.length > 1) { set(typed); return; }
              const arr = value.split("");
              arr[i] = typed;
              set(arr.join("").slice(0, i + 1) + value.slice(i + 1));
            }}
            onKeyDown={(e) => {
              if (e.key === "Backspace" && !d.trim() && i > 0) {
                e.preventDefault();
                onChange(value.slice(0, i - 1));
                refs.current[i - 1]?.focus();
              }
              if (e.key === "ArrowLeft" && i > 0) refs.current[i - 1]?.focus();
              if (e.key === "ArrowRight" && i < 5) refs.current[i + 1]?.focus();
            }}
            onPaste={(e) => {
              const text = e.clipboardData.getData("text");
              if (/\d/.test(text)) { e.preventDefault(); set(text); }
            }}
          />
        </React.Fragment>
      ))}
    </div>
  );
}

/** An identity error as words for an inline line (the API never sends sentences we show raw). */
export function ErrorLine({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="idn-err" role="alert">{error}</div>;
}
