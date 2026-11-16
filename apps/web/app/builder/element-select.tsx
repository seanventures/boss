"use client";

import React, { useId, useState } from "react";

/**
 * Mark one piece of a generated proof. Edit, restyle, comment, or regenerate.
 * The receipt is the record. Sending the same change again does not write another.
 * Palette: pine table, celery proof, persimmon crop marks, brass plate.
 * Faces already on the console: Space Grotesk, Manrope, IBM Plex Mono.
 */

export type ElementAction = "edit" | "restyle" | "comment" | "regenerate";

export type MarkRequest = {
  orgId: string;
  workspaceId: string;
  projectId: string;
  previewId: string;
  elementId: string;
  action: ElementAction;
  change: string;
  actor: string;
};

export type MarkReceipt = {
  id: string;
  dedupeKey: string;
  reused: boolean;
  effectKind: string;
  reservedUsdMicros: number;
  status: "succeeded";
  rendered: string;
  style: string | null;
  comment: string | null;
  effectCount: number;
};

export type MarkClient = {
  apply: (mark: MarkRequest) => Promise<MarkReceipt>;
};

export class MarkError extends Error {
  readonly retryable: boolean;
  readonly deadLetterId?: string;
  readonly code: string;

  constructor(
    message: string,
    opts: { retryable?: boolean; deadLetterId?: string; code?: string } = {},
  ) {
    super(message);
    this.name = "MarkError";
    this.retryable = opts.retryable ?? false;
    this.deadLetterId = opts.deadLetterId;
    this.code = opts.code ?? "effect_failed";
  }
}

const PROOF = [
  { id: "mast", role: "Mast", text: "Hearth & Rye" },
  { id: "headline", role: "Headline", text: "Bread that still remembers the oven." },
  { id: "price", role: "Price", text: "$14 / loaf" },
  { id: "cta", role: "Button", text: "Reserve a Saturday loaf" },
  { id: "note", role: "Note", text: "Baked before dawn. Sold out by ten." },
] as const;

const ACTIONS: { id: ElementAction; label: string; tip: string; help: string }[] = [
  { id: "edit", label: "Edit", tip: "Replace the words", help: "These words replace what is printed on the piece." },
  { id: "restyle", label: "Restyle", tip: "Change the look", help: "Name a look. The printed words stay put." },
  { id: "comment", label: "Comment", tip: "Leave a note", help: "Leave a note. This piece is not rewritten." },
  {
    id: "regenerate",
    label: "Regenerate",
    tip: "Make this piece again",
    help: "This piece is made again from your note.",
  },
];

type Failure = { message: string; retryable: boolean; deadLetterId?: string };

function failureOf(err: unknown): Failure {
  if (err instanceof MarkError) {
    return { message: err.message, retryable: err.retryable, deadLetterId: err.deadLetterId };
  }
  if (err instanceof Error && err.message) return { message: err.message, retryable: true };
  return { message: "The change was not recorded.", retryable: true };
}

function detailOf(body: unknown): Failure & { code: string } {
  const record = body && typeof body === "object" ? (body as { detail?: unknown }) : {};
  const detail = record.detail ?? body;
  if (typeof detail === "string" && detail.trim()) {
    return { message: detail, retryable: true, code: "effect_failed" };
  }
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return { message: "The change was not recorded.", retryable: false, code: "invalid" };
  }
  const row = detail as { message?: unknown; retryable?: unknown; dead_letter_id?: unknown; code?: unknown };
  const message = typeof row.message === "string" && row.message.trim()
    ? row.message
    : "The change was not recorded.";
  return {
    message,
    retryable: row.retryable === true,
    deadLetterId: typeof row.dead_letter_id === "string" ? row.dead_letter_id : undefined,
    code: typeof row.code === "string" ? row.code : "effect_failed",
  };
}

export async function postMark(mark: MarkRequest): Promise<MarkReceipt> {
  let response: Response;
  try {
    response = await fetch("/v1/element-select/apply", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        org_id: mark.orgId,
        workspace_id: mark.workspaceId,
        project_id: mark.projectId,
        preview_id: mark.previewId,
        element_id: mark.elementId,
        action: mark.action,
        change: mark.change,
        actor: mark.actor,
      }),
    });
  } catch (err) {
    throw new MarkError(err instanceof Error && err.message ? err.message : "The change was not recorded.", {
      retryable: true,
      code: "effect_failed",
    });
  }
  const raw = await response.text();
  let body: unknown = {};
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      throw new MarkError("The receipt came back unreadable.", { retryable: true, code: "effect_failed" });
    }
  }
  if (!response.ok) {
    const detail = detailOf(body);
    throw new MarkError(detail.message, {
      retryable: detail.retryable,
      deadLetterId: detail.deadLetterId,
      code: detail.code,
    });
  }
  const row = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  if (typeof row.id !== "string" || typeof row.dedupe_key !== "string") {
    throw new MarkError("The receipt came back unreadable.", { retryable: true, code: "effect_failed" });
  }
  if (typeof row.effect_count !== "number" || typeof row.reserved_usd_micros !== "number") {
    throw new MarkError("The receipt came back unreadable.", { retryable: true, code: "effect_failed" });
  }
  return {
    id: row.id,
    dedupeKey: row.dedupe_key,
    reused: row.reused === true,
    effectKind: typeof row.effect_kind === "string" ? row.effect_kind : "element_edit",
    reservedUsdMicros: row.reserved_usd_micros,
    status: "succeeded",
    rendered: typeof row.rendered === "string" ? row.rendered : "",
    style: typeof row.style === "string" ? row.style : null,
    comment: typeof row.comment === "string" ? row.comment : null,
    effectCount: row.effect_count,
  };
}

const fallbackClient: MarkClient = { apply: postMark };

export function ElementSelect({
  projectId,
  actor = "founder",
  orgId = "local",
  workspaceId = "local",
  previewId = "proof-hearth",
  client,
}: {
  projectId: string;
  actor?: string;
  orgId?: string;
  workspaceId?: string;
  previewId?: string;
  client?: MarkClient;
}) {
  const fieldId = useId();
  const helpId = useId();
  const active = client ?? fallbackClient;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [action, setAction] = useState<ElementAction | null>(null);
  const [change, setChange] = useState("");
  const [texts, setTexts] = useState<Record<string, string>>(() =>
    Object.fromEntries(PROOF.map((piece) => [piece.id, piece.text])),
  );
  const [styles, setStyles] = useState<Record<string, string>>({});
  const [receipt, setReceipt] = useState<MarkReceipt | null>(null);
  const [error, setError] = useState<Failure | null>(null);
  const [pending, setPending] = useState(false);

  const selected = PROOF.find((piece) => piece.id === selectedId) ?? null;
  const help = ACTIONS.find((item) => item.id === action)?.help
    ?? "Choose edit, restyle, comment, or regenerate.";
  const ready = Boolean(selected && action && change.trim()) && !pending;

  async function submit(event?: React.FormEvent) {
    event?.preventDefault();
    if (!selected || !action || !change.trim() || pending) return;
    setPending(true);
    setError(null);
    setReceipt(null);
    try {
      const next = await active.apply({
        orgId,
        workspaceId,
        projectId,
        previewId,
        elementId: selected.id,
        action,
        change,
        actor,
      });
      setReceipt(next);
      if ((action === "edit" || action === "regenerate") && next.rendered) {
        setTexts((prev) => ({ ...prev, [selected.id]: next.rendered }));
      }
      if (action === "restyle" && next.style) {
        setStyles((prev) => ({ ...prev, [selected.id]: next.style as string }));
      }
    } catch (err) {
      setError(failureOf(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="em-root" data-testid="element-select" aria-label="Proof marks">
      <style>{CSS}</style>
      <header className="em-head">
        <p className="em-kicker">Bakery proof</p>
        <h1>Mark the proof</h1>
        <p>
          Point at one piece of this proof. A repeat of the same change keeps the same receipt.
        </p>
      </header>
      <article className="em-sheet" aria-label="Preview proof">
        <div className="em-corners" aria-hidden="true">
          <span /><span /><span /><span />
        </div>
        <p className="em-plate">Plate 04</p>
        {PROOF.map((piece) => (
          <button
            key={piece.id}
            type="button"
            className={`em-piece em-piece-${piece.id}`}
            data-testid={`proof-${piece.id}`}
            data-piece={piece.id}
            data-style={styles[piece.id] || undefined}
            aria-pressed={selectedId === piece.id}
            onClick={() => {
              setSelectedId(piece.id);
              setError(null);
            }}
          >
            <span className="em-role">{piece.role}</span>
            <span className="em-copy" data-user-text>{texts[piece.id]}</span>
          </button>
        ))}
      </article>
      <form className="em-desk" onSubmit={submit} aria-busy={pending}>
        <h2>The mark</h2>
        {selected ? (
          <p>
            <span className="em-label">Selected</span>
            <span data-user-text>{selected.role}</span>
          </p>
        ) : (
          <p>Select a piece on the proof before you record a change.</p>
        )}
        <div className="em-stamps" role="group" aria-label="Change kind">
          {ACTIONS.map((item) => (
            <button
              key={item.id}
              type="button"
              className="em-stamp"
              data-testid={`action-${item.id}`}
              title={item.tip}
              aria-pressed={action === item.id}
              disabled={!selected}
              onClick={() => {
                setAction(item.id);
                setError(null);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p id={helpId} data-testid="mark-help">{help}</p>
        <label className="em-label" htmlFor={fieldId}>Change</label>
        <textarea
          id={fieldId}
          className="em-change"
          data-testid="change-text"
          value={change}
          aria-describedby={helpId}
          placeholder="What should change?"
          onChange={(event) => setChange(event.target.value)}
        />
        <button
          type="submit"
          className="em-record"
          data-testid="record-change"
          title="Write one receipt"
          disabled={!ready}
        >
          Record change
        </button>
        {error ? (
          <p className="em-error" role="alert" data-testid="mark-error">{error.message}</p>
        ) : null}
        {error?.deadLetterId ? (
          <p data-testid="dead-letter">
            Dead letter <span data-user-text>{error.deadLetterId}</span>
          </p>
        ) : null}
        {error?.retryable ? (
          <button type="button" className="em-retry" data-testid="mark-retry" onClick={() => submit()}>
            Retry
          </button>
        ) : null}
        {receipt ? (
          <aside
            className="em-slip"
            data-testid="element-receipt"
            data-reused={receipt.reused ? "true" : "false"}
            role="status"
          >
            <p className="em-slip-state">{receipt.reused ? "Already recorded" : "Recorded once"}</p>
            <p>
              <span className="em-label">Receipt</span>
              <span data-testid="receipt-id" data-user-text>{receipt.id}</span>
            </p>
            <p>
              <span className="em-label">Dedupe key</span>
              <span data-testid="dedupe-key" data-user-text>{receipt.dedupeKey}</span>
            </p>
            <p>
              <span className="em-label">Effects</span>
              <span data-testid="effect-count">{receipt.effectCount}</span>
            </p>
            <p>
              <span className="em-label">Held</span>
              <span data-testid="held-micros">{receipt.reservedUsdMicros}</span>
            </p>
            {receipt.comment ? <p data-user-text>{receipt.comment}</p> : null}
          </aside>
        ) : null}
      </form>
    </section>
  );
}

const CSS = `
.em-root {
  --em-pine: #1e2a26;
  --em-paper: #f4f7f2;
  --em-ink: #1c1915;
  --em-persimmon: #d85a2a;
  --em-brass: #c9a15b;
  --em-sage: #7e9a78;
  --em-ember: #b8431c;
  box-sizing: border-box;
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(16rem, 0.8fr);
  grid-template-areas: "head head" "proof desk";
  gap: 18px 22px;
  min-height: 100%;
  padding: 28px 24px 36px;
  background: var(--em-pine);
  color: var(--em-paper);
  font-family: "Manrope", var(--sans, ui-sans-serif), sans-serif;
}
.em-root *, .em-root *::before, .em-root *::after { box-sizing: border-box; }
.em-head { grid-area: head; max-width: 38rem; }
.em-kicker, .em-plate {
  margin: 0 0 6px;
  color: var(--em-brass);
  font-family: "IBM Plex Mono", "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.em-root h1 {
  margin: 0 0 8px;
  font-family: "Space Grotesk", "Manrope", var(--display, sans-serif), sans-serif;
  font-size: 2.15rem;
  font-weight: 500;
  letter-spacing: -0.03em;
  line-height: 1.05;
}
.em-root h2 {
  margin: 0 0 10px;
  font-family: "Space Grotesk", "Manrope", sans-serif;
  font-size: 1.25rem;
  font-weight: 500;
}
.em-head p, .em-desk p { margin: 0 0 12px; line-height: 1.45; }
.em-sheet {
  grid-area: proof;
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
  padding: 36px 28px 32px;
  background: var(--em-paper);
  color: var(--em-ink);
}
.em-corners span {
  position: absolute;
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--em-brass);
}
.em-corners span:nth-child(1) { top: 10px; left: 10px; border-right: 0; border-bottom: 0; }
.em-corners span:nth-child(2) { top: 10px; right: 10px; border-left: 0; border-bottom: 0; }
.em-corners span:nth-child(3) { bottom: 10px; left: 10px; border-right: 0; border-top: 0; }
.em-corners span:nth-child(4) { bottom: 10px; right: 10px; border-left: 0; border-top: 0; }
.em-plate { position: absolute; top: 12px; right: 28px; }
.em-piece {
  position: relative;
  max-width: 100%;
  padding: 2px 4px;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.em-role {
  display: block;
  margin-bottom: 2px;
  color: #5e6b57;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.em-piece-mast .em-copy, .em-piece-headline .em-copy {
  font-family: "Space Grotesk", "Palatino Linotype", Palatino, serif;
}
.em-piece-mast .em-copy { font-size: 1.05rem; letter-spacing: 0.14em; text-transform: uppercase; }
.em-piece-headline .em-copy { font-size: 1.85rem; font-weight: 500; letter-spacing: -0.03em; line-height: 1.15; }
.em-piece-price .em-copy {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 1.35rem;
}
.em-piece-cta .em-copy {
  display: inline-block;
  padding: 8px 12px;
  border: 1.5px solid var(--em-ink);
}
.em-piece-note .em-copy { font-size: 0.95rem; }
.em-piece[aria-pressed="true"]::before,
.em-piece[aria-pressed="true"]::after {
  content: "";
  position: absolute;
  width: 10px;
  height: 10px;
  border-color: var(--em-persimmon);
  border-style: solid;
  animation: em-crop 160ms ease-out;
}
.em-piece[aria-pressed="true"]::before { top: -6px; left: -6px; border-width: 2px 0 0 2px; }
.em-piece[aria-pressed="true"]::after { right: -6px; bottom: -6px; border-width: 0 2px 2px 0; }
.em-piece[data-style="ember"] { color: var(--em-ember); }
.em-piece[data-style="flour"] { background: #f3e6c4; }
.em-piece[data-style="pine"] { background: #dce8df; }
.em-piece[data-style="ink"] { background: var(--em-ink); color: var(--em-paper); }
.em-piece[data-style="marked"] { outline: 1px dashed var(--em-brass); }
.em-desk {
  grid-area: desk;
  border-left: 1px solid var(--em-brass);
  padding-left: 18px;
}
.em-label {
  display: block;
  margin-bottom: 4px;
  color: var(--em-brass);
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.em-stamps { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.em-stamp, .em-record, .em-retry { cursor: pointer; }
.em-stamp {
  border: 1px solid var(--em-sage);
  border-radius: 999px;
  padding: 6px 12px;
  background: transparent;
  color: var(--em-paper);
  font: 600 0.82rem/1 "Manrope", sans-serif;
}
.em-stamp[aria-pressed="true"] {
  border-color: var(--em-persimmon);
  background: var(--em-persimmon);
  color: var(--em-ink);
}
.em-stamp:disabled { opacity: 0.4; cursor: not-allowed; }
.em-change {
  width: 100%;
  min-height: 5.5rem;
  margin: 0 0 12px;
  padding: 10px;
  border: 1px solid var(--em-sage);
  border-radius: 4px;
  background: #16211e;
  color: var(--em-paper);
  font: 500 0.95rem/1.4 "Manrope", sans-serif;
  resize: vertical;
}
.em-record {
  border: 0;
  border-radius: 4px;
  padding: 10px 14px;
  background: var(--em-paper);
  color: var(--em-pine);
  font: 600 0.92rem/1 "Manrope", sans-serif;
}
.em-record:disabled { opacity: 0.45; cursor: not-allowed; }
.em-error { color: #ffb4a2; }
.em-retry {
  margin-bottom: 12px;
  border: 1px solid var(--em-persimmon);
  border-radius: 4px;
  padding: 8px 12px;
  background: transparent;
  color: var(--em-paper);
  font: 600 0.85rem/1 "Manrope", sans-serif;
}
.em-slip {
  margin-top: 8px;
  padding: 18px 12px 12px;
  color: var(--em-ink);
  background:
    radial-gradient(circle at 10px 0, var(--em-pine) 7px, transparent 8px) 0 0 / 20px 12px repeat-x,
    var(--em-paper);
  font-family: "IBM Plex Mono", "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.78rem;
}
.em-slip .em-label { color: #8c6a32; }
.em-slip-state { font-family: "Space Grotesk", "Manrope", sans-serif; font-size: 1rem; }
.em-piece:focus-visible, .em-stamp:focus-visible, .em-record:focus-visible,
.em-retry:focus-visible, .em-change:focus-visible {
  outline: 2px solid var(--em-persimmon);
  outline-offset: 3px;
}
@keyframes em-crop { from { opacity: 0; } to { opacity: 1; } }
@media (max-width: 800px) {
  .em-root {
    grid-template-columns: 1fr;
    grid-template-areas: "head" "proof" "desk";
    padding: 16px;
  }
  .em-desk { border-left: 0; border-top: 1px solid var(--em-brass); padding: 16px 0 0; }
  .em-piece-headline .em-copy { font-size: 1.45rem; }
}
@media (prefers-reduced-motion: reduce) {
  .em-piece[aria-pressed="true"]::before,
  .em-piece[aria-pressed="true"]::after { animation: none; }
}
`;
