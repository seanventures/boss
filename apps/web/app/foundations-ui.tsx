"use client";

/**
 * BOSS-281 — Foundations: the design-system reference page from the handoff
 * (~/Documents/design_handoff_boss_design_system), restored as a console area.
 *
 * Faithfulness rule: every swatch/type/status sample renders from the LIVE CSS custom
 * properties in design-system.css (var(--void) etc.), never hardcoded hex — so this page
 * *is* the implementation, and cannot drift from it. Sections mirror the prototype:
 * 01 Color · 02 Typography · 03 Motion · 04 Principles.
 */

const SURFACES = [
  { token: "--void", label: "Void", note: "app background" },
  { token: "--base", label: "Base", note: "behind panels" },
  { token: "--panel", label: "Panel", note: "panel bodies" },
  { token: "--card", label: "Card", note: "board / list cards" },
  { token: "--raised", label: "Raised", note: "raised fills" },
];

const SIGNALS = [
  { token: "--teal", label: "RUNNING", pulse: true },
  { token: "--green", label: "VERIFIED" },
  { token: "--amber", label: "AWAITING" },
  { token: "--red", label: "BLOCKED" },
];

function Swatch({ token, label, note }: { token: string; label: string; note?: string }) {
  return (
    <div style={{ display: "grid", gap: 5 }}>
      <div
        style={{
          height: 56,
          background: `var(${token})`,
          border: "1px solid var(--border-panel, #1c2740)",
          borderRadius: 8,
        }}
      />
      <div style={{ fontSize: 12.5 }}>
        <b>{label}</b>{" "}
        <span style={{ fontFamily: "var(--mono, monospace)", color: "var(--muted, #64769a)" }}>
          var({token})
        </span>
        {note ? (
          <span style={{ color: "var(--muted, #64769a)" }}> — {note}</span>
        ) : null}
      </div>
    </div>
  );
}

export function FoundationsHome() {
  return (
    <div className="stack-grid" style={{ gap: 16 }}>
      {/* hero */}
      <article className="stack-card">
        <span
          style={{
            fontFamily: "var(--mono, monospace)", fontSize: 10.5, letterSpacing: "0.22em",
            textTransform: "uppercase", color: "var(--teal, #2ec5c5)",
          }}
        >
          BOSS / Design System
        </span>
        <h2
          style={{
            margin: "6px 0 4px", fontFamily: "var(--display, Sora, sans-serif)",
            fontWeight: 800, letterSpacing: "-0.02em", fontSize: 30,
            background: "var(--brand-grad, linear-gradient(135deg,#2ec5c5,#2f6fe0 55%,#1b3a6b))",
            WebkitBackgroundClip: "text", backgroundClip: "text", color: "transparent",
          }}
        >
          Run a company of AI workers. Stay in command.
        </h2>
        <p className="detail" style={{ maxWidth: "62ch" }}>
          The visual language for BOSS — an operating system for AI labor. Flat surfaces,
          hairline structure, accountable agents. Powerful without being loud; the operator is
          always in control. Everything on this page renders from the live tokens in
          design-system.css — it cannot drift from the product.
        </p>
      </article>

      {/* 01 color */}
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>01 · Color</h3>
        <p className="detail">Surfaces (dark), the brand gradient, and the signal palette.</p>
        <div
          style={{
            display: "grid", gap: 12,
            gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
          }}
        >
          {SURFACES.map((s) => (
            <Swatch key={s.token} {...s} />
          ))}
          <div style={{ display: "grid", gap: 5 }}>
            <div
              style={{
                height: 56, borderRadius: 8,
                background:
                  "var(--brand-grad, linear-gradient(135deg,#2ec5c5,#2f6fe0 55%,#1b3a6b))",
              }}
            />
            <div style={{ fontSize: 12.5 }}>
              <b>Brand</b>{" "}
              <span style={{ fontFamily: "var(--mono, monospace)", color: "var(--muted)" }}>
                teal → navy
              </span>
            </div>
          </div>
        </div>
        <p className="detail" style={{ marginTop: 12, marginBottom: 6 }}>
          Signal — agent/work state is <b>text, not pills</b>: uppercase mono in the state
          color, with an optional dot. Only running state pulses.
        </p>
        <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
          {SIGNALS.map((s) => (
            <span
              key={s.label}
              style={{
                fontFamily: "var(--mono, monospace)", fontWeight: 700, fontSize: 12,
                letterSpacing: "0.06em", color: `var(${s.token})`,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <span
                aria-hidden
                className={s.pulse ? "foundations-pulse" : undefined}
                style={{
                  width: 7, height: 7, borderRadius: 999, background: `var(${s.token})`,
                  display: "inline-block",
                }}
              />
              {s.label}
            </span>
          ))}
        </div>
      </article>

      {/* 02 typography */}
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>02 · Typography</h3>
        <div style={{ display: "grid", gap: 14, marginTop: 8 }}>
          <div>
            <span className="detail">Sora — display. 700–800, tracking −0.02em.</span>
            <div
              style={{
                fontFamily: "var(--display, Sora, sans-serif)", fontWeight: 800,
                fontSize: 34, letterSpacing: "-0.02em", lineHeight: 1.1,
              }}
            >
              Assign the outcome
            </div>
          </div>
          <div>
            <span className="detail">Manrope — interface &amp; body. 400–700.</span>
            <div style={{ fontSize: 15.5, maxWidth: "58ch" }}>
              The architect drafted a schema and routed it to QA. Frozen tests were written
              before implementation; the review panel refutes, the gate decides.
            </div>
          </div>
          <div>
            <span className="detail">
              JetBrains Mono — data, receipts, IDs, timestamps, status, eyebrows.
            </span>
            <div
              style={{
                fontFamily: "var(--mono, monospace)", fontSize: 12.5,
                color: "var(--muted, #64769a)",
              }}
            >
              rcpt_7f3a92c1 · 2026-07-26T09:14:02Z ·{" "}
              <span style={{ color: "var(--green, #34d399)", fontWeight: 700 }}>VERIFIED</span>
            </div>
          </div>
        </div>
      </article>

      {/* 03 motion */}
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>03 · Motion</h3>
        <p className="detail" style={{ maxWidth: "62ch" }}>
          Motion means <b>liveness, never decoration</b>: only live things move — running work
          pulses, working agents orbit, handoffs flow. Nothing else animates, and everything
          respects <code style={{ fontFamily: "var(--mono, monospace)" }}>prefers-reduced-motion</code>.
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            aria-hidden
            className="foundations-pulse"
            style={{
              width: 9, height: 9, borderRadius: 999,
              background: "var(--teal, #2ec5c5)", display: "inline-block",
            }}
          />
          <span
            style={{
              fontFamily: "var(--mono, monospace)", fontWeight: 700, fontSize: 12,
              letterSpacing: "0.06em", color: "var(--teal, #2ec5c5)",
            }}
          >
            RUNNING
          </span>
          <span className="detail">— the one moving element on this page.</span>
        </div>
        <style>{`
          .foundations-pulse { animation: foundations-pulse 1.6s ease-in-out infinite; }
          @keyframes foundations-pulse {
            0%, 100% { opacity: 1; } 50% { opacity: 0.35; }
          }
          @media (prefers-reduced-motion: reduce) {
            .foundations-pulse { animation: none; }
          }
        `}</style>
      </article>

      {/* 04 principles */}
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>04 · Principles</h3>
        <div style={{ display: "grid", gap: 8, marginTop: 6 }}>
          {[
            ["Flat, hairline-structured surfaces",
             "Bordered rectangles separated by 1px hairlines — no bento grids, no pill chips, no nested rounding."],
            ["Status is text, not pills",
             "Colored uppercase mono (RUNNING · VERIFIED · BLOCKED) with an optional dot — never a filled pill."],
            ["Motion = liveness",
             "Only live things move. Pulse for running, orbit for working agents, flow for handoffs."],
            ["Human in control",
             "The operator identity is always present; irreversible actions confirm; proposals wait in the Decision Queue."],
            ["Calm power",
             "Generous type, restrained color, one brand gradient used sparingly."],
          ].map(([title, body]) => (
            <div
              key={title}
              style={{ borderBottom: "1px solid var(--hair, #131d34)", paddingBottom: 8 }}
            >
              <b style={{ fontSize: 13.5 }}>{title}</b>
              <p className="detail" style={{ margin: "2px 0 0" }}>{body}</p>
            </div>
          ))}
        </div>
      </article>
    </div>
  );
}
