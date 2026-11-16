"use client";

import React, { useCallback, useEffect, useState } from "react";

import { apiWrite } from "../../lib/api-write";

type Kind = "file" | "table";

type FreezeRow = {
  kind: Kind;
  path: string;
  frozen: boolean;
  frozen_by?: string;
};

type WriteResult = { ok: boolean; detail: string };

function detailOf(body: { detail?: unknown }): string {
  const detail = body.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return "";
}

async function readResult(res: Response): Promise<WriteResult> {
  const body = await res.json().catch(() => ({})) as { detail?: unknown };
  const detail = detailOf(body);
  if (!res.ok) return { ok: false, detail: detail || "The write did not finish." };
  return { ok: true, detail };
}

/**
 * A builder holds files and data tables still. A frozen path refuses a write.
 * An open path can still change.
 */
export function FreezePaths({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<FreezeRow[]>([]);
  const [kind, setKind] = useState<Kind>("file");
  const [path, setPath] = useState("");
  const [openBytes, setOpenBytes] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/v1/freeze-paths?project_id=${encodeURIComponent(projectId)}`, {
        cache: "no-store",
      });
      const body = await res.json().catch(() => ({})) as { paths?: FreezeRow[]; detail?: unknown };
      if (!res.ok) {
        setLoadError(detailOf(body) || "The freeze list did not load.");
        return;
      }
      setRows(Array.isArray(body.paths) ? body.paths : []);
      setLoadError(null);
    } catch {
      setLoadError("The freeze list did not load.");
    } finally {
      setLoaded(true);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  async function freeze() {
    const res = await apiWrite("/v1/freeze-paths", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, kind, path }),
    }, { press: { scope: `freeze:${projectId}`, label: "Freeze" } });
    const result = await readResult(res);
    if (!result.ok) {
      setNote(result.detail);
      return;
    }
    setNote(null);
    await load();
  }

  async function thaw(row: FreezeRow) {
    const res = await apiWrite("/v1/freeze-paths/thaw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, kind: row.kind, path: row.path }),
    }, { press: { scope: `freeze:${projectId}`, label: "Thaw" } });
    const result = await readResult(res);
    if (!result.ok) {
      setNote(result.detail);
      return;
    }
    setNote(null);
    await load();
  }

  async function attempt(writeKind: string, writePath: string, content: string) {
    const res = await apiWrite("/v1/freeze-paths/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId, kind: writeKind, path: writePath, content,
      }),
    }, { press: { scope: `freeze:${projectId}`, label: "Try a write" } });
    const result = await readResult(res);
    if (!result.ok) {
      setNote(result.detail);
      return;
    }
    setNote("The path changed.");
  }

  return (
    <section className="fp-hold" data-testid="freeze-paths">
      <style>{CSS}</style>
      <header className="fp-mast">
        <p className="fp-eyebrow">Builder hold</p>
        <h1>Hold these paths.</h1>
        <p>A frozen file or table refuses a write. The bytes stay as they were.</p>
        <p>An open path can still change.</p>
      </header>

      <form className="fp-bench" onSubmit={(event) => event.preventDefault()}>
        <label className="fp-field">
          <span>Kind</span>
          <select
            data-testid="freeze-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value === "table" ? "table" : "file")}
          >
            <option value="file">File</option>
            <option value="table">Table</option>
          </select>
        </label>
        <label className="fp-field fp-path">
          <span>Path</span>
          <input
            data-testid="freeze-path"
            value={path}
            spellCheck={false}
            onChange={(event) => setPath(event.target.value)}
            placeholder="notes.txt"
          />
        </label>
        <button
          type="button"
          data-testid="freeze-submit"
          title="Freeze this path"
          onClick={() => { void freeze(); }}
        >
          Freeze
        </button>
        <label className="fp-field fp-bytes">
          <span>New bytes</span>
          <input
            data-testid="open-bytes"
            value={openBytes}
            spellCheck={false}
            aria-label="New bytes"
            onChange={(event) => setOpenBytes(event.target.value)}
          />
        </label>
        <button
          type="button"
          data-testid="open-try"
          title="Send a write to this path"
          onClick={() => { void attempt(kind, path, openBytes); }}
        >
          Try a write
        </button>
      </form>

      {loadError ? <p data-testid="freeze-load-error">{loadError}</p> : null}
      {note ? <p data-testid="freeze-result" role="status">{note}</p> : null}

      {loaded && rows.length === 0 ? (
        <p data-testid="freeze-empty">Nothing is held. Name a file or a table to freeze it.</p>
      ) : null}

      {rows.length > 0 ? (
        <ol className="fp-drawer">
          {rows.map((row) => {
            const key = `${row.kind}:${row.path}`;
            return (
              <li
                key={key}
                className="fp-slide"
                data-testid="freeze-slide"
                data-kind={row.kind}
                data-path={row.path}
              >
                <span className="fp-kind">{row.kind === "table" ? "Table" : "File"}</span>
                <code className="fp-name">{row.path}</code>
                <span className="fp-seal" title="Held. A write is refused.">HOLD</span>
                <button
                  type="button"
                  data-testid="freeze-thaw"
                  title="Let this path change"
                  onClick={() => { void thaw(row); }}
                >
                  Thaw
                </button>
                <input
                  data-testid="freeze-bytes"
                  aria-label={`Bytes for ${row.path}`}
                  value={drafts[key] ?? ""}
                  spellCheck={false}
                  onChange={(event) => setDrafts((prev) => ({ ...prev, [key]: event.target.value }))}
                />
                <button
                  type="button"
                  data-testid="freeze-try"
                  title="Send a write to this path"
                  onClick={() => { void attempt(row.kind, row.path, drafts[key] ?? ""); }}
                >
                  Try a write
                </button>
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}

const CSS = `
.fp-hold {
  --paper: #e7f1ec;
  --steel: #14312c;
  --ink: #10211e;
  --frost: #f4fbf8;
  --seal: #9c2f2a;
  --line: #c5d6ce;
  --open: #2a6a5c;
  background:
    radial-gradient(1200px 240px at 0% 0%, rgba(20, 49, 44, 0.06), transparent 60%),
    var(--paper);
  color: var(--ink);
  font-family: "Source Serif 4", Palatino, "Palatino Linotype", serif;
  padding: 32px 36px 56px;
  min-height: 100%;
}
.fp-hold * { box-sizing: border-box; }
.fp-eyebrow {
  margin: 0 0 8px;
  font-family: "Familjen Grotesk", "Avenir Next", sans-serif;
  font-size: 0.72rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--open);
}
.fp-hold h1 {
  margin: 0 0 12px;
  font-family: "Familjen Grotesk", "Avenir Next", sans-serif;
  font-weight: 700;
  font-size: clamp(2rem, 4vw, 3.1rem);
  letter-spacing: -0.04em;
  line-height: 0.95;
}
.fp-mast p { max-width: 38rem; margin: 0 0 8px; font-size: 1.05rem; line-height: 1.45; }
.fp-bench {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr) auto;
  gap: 12px 14px;
  align-items: end;
  margin: 22px 0 18px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
}
.fp-field { display: flex; flex-direction: column; gap: 6px; }
.fp-field span, .fp-kind {
  font-family: "Familjen Grotesk", "Avenir Next", sans-serif;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.fp-hold input, .fp-hold select {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.92rem;
  color: var(--ink);
  background: var(--frost);
  border: 1px solid var(--line);
  border-radius: 0;
  padding: 10px 12px;
}
.fp-hold button {
  font-family: "Familjen Grotesk", "Avenir Next", sans-serif;
  font-weight: 500;
  letter-spacing: 0.02em;
  background: var(--steel);
  color: var(--frost);
  border: 0;
  border-radius: 0;
  padding: 11px 16px;
  cursor: pointer;
}
.fp-hold button:focus-visible,
.fp-hold input:focus-visible,
.fp-hold select:focus-visible {
  outline: 2px solid var(--steel);
  outline-offset: 2px;
}
.fp-bytes { grid-column: 1 / 3; }
.fp-drawer { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; }
.fp-slide {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) auto auto;
  grid-template-areas: "kind name seal thaw" "kind bytes bytes try";
  gap: 10px 12px;
  align-items: center;
  margin: 0;
  padding: 14px 14px 14px 16px;
  background: linear-gradient(180deg, rgba(255,255,255,0.55), transparent 42%), var(--frost);
  border: 1px solid var(--line);
  border-left: 0;
  border-top: 0;
  box-shadow: inset 6px 0 0 var(--seal);
}
.fp-slide:first-child { border-top: 1px solid var(--line); }
.fp-kind { grid-area: kind; color: var(--seal); }
.fp-name { grid-area: name; font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.95rem; overflow-wrap: anywhere; }
.fp-seal {
  grid-area: seal;
  width: 52px;
  height: 52px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #c85b54, var(--seal) 58%, #6d1d19);
  color: #f4fbf8;
  font-family: "Familjen Grotesk", "Avenir Next", sans-serif;
  font-size: 0.62rem;
  letter-spacing: 0.14em;
  transform: rotate(-8deg);
  box-shadow: inset 0 0 0 2px rgba(244, 251, 248, 0.35);
}
.fp-slide [data-testid="freeze-thaw"] { grid-area: thaw; background: transparent; color: var(--open); border: 1px solid var(--open); }
.fp-slide input { grid-area: bytes; }
.fp-slide [data-testid="freeze-try"] { grid-area: try; }
.fp-hold [data-testid="freeze-result"],
.fp-hold [data-testid="freeze-empty"],
.fp-hold [data-testid="freeze-load-error"] {
  max-width: 40rem;
  font-size: 1.02rem;
}
@media (max-width: 720px) {
  .fp-hold { padding: 20px 16px 40px; }
  .fp-bench { grid-template-columns: 1fr; }
  .fp-bytes { grid-column: auto; }
  .fp-slide {
    grid-template-columns: 1fr auto;
    grid-template-areas: "kind seal" "name name" "bytes bytes" "thaw try";
  }
}
@media (prefers-reduced-motion: reduce) {
  .fp-seal { transform: none; }
}
`;
