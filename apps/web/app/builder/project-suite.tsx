"use client";

/**
 * One press sheet: a website, a mobile app, slides, and a data view locked in a chase.
 * Publishing one plate does not take the others off the sheet.
 */
import React, { useEffect, useRef, useState } from "react";

export const SUITE_KINDS = ["website", "mobile", "slides", "data_view"] as const;

export type SuiteKind = (typeof SUITE_KINDS)[number];

export type SuiteSurface = {
  kind: SuiteKind;
  title: string;
  body: string;
  status: "draft" | "published";
  revision: number;
  published_revision: number | null;
};

export type PublishRequest = {
  mode: "one" | "together";
  kind?: SuiteKind;
  dedupe_key: string;
};

export type HoldRequest = {
  kind: SuiteKind;
  title: string;
  body: string;
};

export type SuitePayload = {
  project_id?: string;
  project_name?: string;
  surfaces?: SuiteSurface[];
  message?: string;
};

const LABEL: Record<SuiteKind, string> = {
  website: "Website",
  mobile: "Mobile app",
  slides: "Slides",
  data_view: "Data view",
};

const CODE: Record<SuiteKind, string> = {
  website: "WEB",
  mobile: "APP",
  slides: "SLD",
  data_view: "DAT",
};

export function suiteDedupeKey(
  mode: "one" | "together",
  kind: string | null,
  surfaces: { kind: string; revision: number; status: string }[],
): string {
  const material = [...surfaces]
    .sort((a, b) => a.kind.localeCompare(b.kind))
    .map((row) => `${row.kind}:${row.revision}:${row.status}`)
    .join("|");
  return `suite:${mode}:${kind ?? "sheet"}:${material}`;
}

/** A partial reply updates the plates it names and leaves every other plate where it was. */
export function keepSurfaces(previous: SuiteSurface[], incoming: SuiteSurface[] | undefined): SuiteSurface[] {
  const next = new Map(previous.map((row) => [row.kind, row]));
  for (const row of incoming ?? []) {
    if ((SUITE_KINDS as readonly string[]).includes(row.kind)) next.set(row.kind, row);
  }
  return SUITE_KINDS.flatMap((kind) => {
    const row = next.get(kind);
    return row ? [row] : [];
  });
}

async function readPayload(res: Response): Promise<SuitePayload> {
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const message = data && typeof data.message === "string" && data.message
      ? data.message
      : "The suite could not be saved.";
    throw new Error(message);
  }
  if (!data || typeof data !== "object") throw new Error("The suite came back empty.");
  return data as SuitePayload;
}

async function publishRemote(projectId: string, body: PublishRequest): Promise<SuitePayload> {
  const res = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/suite/publish`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return readPayload(res);
}

async function holdRemote(projectId: string, body: HoldRequest): Promise<SuitePayload> {
  const res = await fetch(
    `/v1/projects/${encodeURIComponent(projectId)}/suite/surfaces/${encodeURIComponent(body.kind)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: body.title, body: body.body }),
    },
  );
  return readPayload(res);
}

async function loadRemote(projectId: string): Promise<SuitePayload> {
  const res = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/suite`, { cache: "no-store" });
  return readPayload(res);
}

function messageOf(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "The suite could not be saved.";
}

export function ProjectSuite({
  projectId = "",
  projectName = "",
  initial,
  onPublish,
  onHold,
}: {
  projectId?: string;
  projectName?: string;
  initial?: SuiteSurface[];
  onPublish?: (req: PublishRequest) => Promise<SuitePayload>;
  onHold?: (req: HoldRequest) => Promise<SuitePayload>;
}) {
  const remote = initial === undefined;
  const [surfaces, setSurfaces] = useState<SuiteSurface[]>(initial ?? []);
  const [name, setName] = useState(projectName);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const drafts = useRef<Record<string, HTMLInputElement | null>>({});

  useEffect(() => {
    if (!remote || !projectId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await loadRemote(projectId);
        if (cancelled) return;
        setSurfaces((prev) => keepSurfaces(prev, data.surfaces));
        if (data.project_name) setName(data.project_name);
      } catch (err) {
        if (!cancelled) setError(messageOf(err));
      }
    })();
    return () => { cancelled = true; };
  }, [remote, projectId]);

  const byKind = new Map(surfaces.map((row) => [row.kind, row]));
  const allHeld = SUITE_KINDS.every((kind) => byKind.has(kind));

  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  function publishOne(kind: SuiteKind) {
    const body: PublishRequest = {
      mode: "one",
      kind,
      dedupe_key: suiteDedupeKey("one", kind, surfaces),
    };
    void run(async () => {
      const data = await (onPublish ?? ((req) => publishRemote(projectId, req)))(body);
      setSurfaces((prev) => keepSurfaces(prev, data.surfaces));
    });
  }

  function publishSheet() {
    const body: PublishRequest = {
      mode: "together",
      dedupe_key: suiteDedupeKey("together", null, surfaces),
    };
    void run(async () => {
      const data = await (onPublish ?? ((req) => publishRemote(projectId, req)))(body);
      setSurfaces((prev) => keepSurfaces(prev, data.surfaces));
    });
  }

  function hold(kind: SuiteKind) {
    const title = drafts.current[kind]?.value.trim() ?? "";
    if (!title) {
      setError("A surface needs a title.");
      return;
    }
    const body: HoldRequest = { kind, title, body: "" };
    void run(async () => {
      const data = await (onHold ?? ((req) => holdRemote(projectId, req)))(body);
      setSurfaces((prev) => keepSurfaces(prev, data.surfaces));
    });
  }

  return (
    <section className="psu" data-testid="project-suite" aria-label="Project suite">
      <style>{CSS}</style>
      <span className="psu-cross psu-cross-nw" aria-hidden="true" />
      <span className="psu-cross psu-cross-ne" aria-hidden="true" />
      <span className="psu-cross psu-cross-sw" aria-hidden="true" />
      <span className="psu-cross psu-cross-se" aria-hidden="true" />
      <header className="psu-head">
        <span className="psu-kicker">Press sheet</span>
        <h1>{name || "Untitled project"}</h1>
        <p className="psu-lead" data-testid="suite-lead">
          A project holds a website, a mobile app, slides, and a data view.
          Publishing one leaves the others in place.
        </p>
      </header>
      {!projectId && remote ? (
        <p className="psu-note">Name a project to open its sheet.</p>
      ) : null}
      <div className="psu-chase" data-testid="suite-chase">
        {SUITE_KINDS.map((kind) => {
          const surface = byKind.get(kind);
          return (
            <article
              key={kind}
              className="psu-plate"
              data-kind={kind}
              data-status={surface ? surface.status : "empty"}
              data-testid={`suite-plate-${kind}`}
            >
              <div className="psu-plate-top">
                <span className="psu-code">{CODE[kind]}</span>
                <h2>{LABEL[kind]}</h2>
                <span className="psu-status" data-testid={`suite-status-${kind}`}>
                  {surface ? (surface.status === "published" ? "Published" : "Held") : "Not held"}
                </span>
              </div>
              {surface ? (
                <>
                  <p className="psu-title" data-testid={`suite-title-${kind}`}>{surface.title}</p>
                  {surface.body ? <p className="psu-body">{surface.body}</p> : null}
                  <button
                    type="button"
                    data-testid={`suite-publish-${kind}`}
                    title="Publish this plate only."
                    disabled={busy}
                    onClick={() => publishOne(kind)}
                  >
                    Publish
                  </button>
                </>
              ) : (
                <div className="psu-hold">
                  <input
                    ref={(node) => { drafts.current[kind] = node; }}
                    data-testid={`suite-title-input-${kind}`}
                    aria-label={`${LABEL[kind]} title`}
                    defaultValue=""
                  />
                  <button
                    type="button"
                    data-testid={`suite-hold-${kind}`}
                    title="Keep this plate on the sheet."
                    disabled={busy}
                    onClick={() => hold(kind)}
                  >
                    Hold
                  </button>
                </div>
              )}
            </article>
          );
        })}
      </div>
      <div className="psu-bar">
        <button
          type="button"
          data-testid="suite-publish-together"
          title={allHeld ? "Publish every held plate." : "Hold all four first."}
          disabled={!allHeld || busy}
          onClick={publishSheet}
        >
          Publish the sheet
        </button>
        {allHeld ? null : (
          <p className="psu-note" data-testid="suite-sheet-note">
            Hold all four plates before publishing the sheet.
          </p>
        )}
        {error ? <p className="psu-error" data-testid="suite-error" role="alert">{error}</p> : null}
      </div>
    </section>
  );
}

const CSS = `
.psu {
  --psu-zinc: #d5dde3;
  --psu-ink: #1c2a32;
  --psu-sheet: #f4f7f8;
  --psu-chase: #1e2c33;
  --psu-muted: #5c6e78;
  --psu-web: #0e7490;
  --psu-app: #be3a5a;
  --psu-slides: #a16207;
  --psu-data: #1f6b4a;
  position: relative;
  box-sizing: border-box;
  margin: 0;
  padding: 28px 22px 32px;
  background: var(--psu-zinc);
  color: var(--psu-ink);
  font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
}
.psu *, .psu *::before, .psu *::after { box-sizing: border-box; }
.psu-cross { position: absolute; width: 13px; height: 13px; }
.psu-cross::before, .psu-cross::after {
  content: "";
  position: absolute;
  background: var(--psu-chase);
}
.psu-cross::before { left: 6px; top: 0; width: 1px; height: 13px; }
.psu-cross::after { top: 6px; left: 0; height: 1px; width: 13px; }
.psu-cross-nw { top: 10px; left: 10px; }
.psu-cross-ne { top: 10px; right: 10px; }
.psu-cross-sw { bottom: 10px; left: 10px; }
.psu-cross-se { bottom: 10px; right: 10px; }
.psu-head { max-width: 40rem; margin: 0 0 18px; }
.psu-kicker {
  display: block;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--psu-muted);
}
.psu h1 {
  margin: 4px 0 8px;
  font-family: Syne, "Avenir Next", "Segoe UI", sans-serif;
  font-weight: 700;
  font-size: 2.4rem;
  line-height: 0.95;
  letter-spacing: -0.04em;
}
.psu h2 { margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: 0; }
.psu-lead, .psu-note, .psu-body, .psu-title, .psu-error { margin: 0; }
.psu-lead { max-width: 38rem; line-height: 1.45; }
.psu-chase {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 2px;
  background: var(--psu-chase);
  padding: 2px;
}
.psu-plate {
  min-width: 0;
  min-height: 11rem;
  padding: 12px 12px 14px;
  background: var(--psu-sheet);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.psu-plate[data-kind="website"] { box-shadow: inset 0 4px 0 var(--psu-web); }
.psu-plate[data-kind="mobile"] { box-shadow: inset 0 4px 0 var(--psu-app); }
.psu-plate[data-kind="slides"] { box-shadow: inset 0 4px 0 var(--psu-slides); }
.psu-plate[data-kind="data_view"] { box-shadow: inset 0 4px 0 var(--psu-data); }
.psu-plate-top { display: flex; align-items: baseline; gap: 8px; }
.psu-code {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  color: var(--psu-muted);
}
.psu-status { margin-left: auto; font-size: 0.75rem; color: var(--psu-muted); }
.psu-plate[data-status="published"] .psu-status { color: var(--psu-ink); font-weight: 600; }
.psu-title { font-size: 1.05rem; line-height: 1.3; }
.psu-body { color: var(--psu-muted); font-size: 0.88rem; line-height: 1.4; }
.psu-hold { display: flex; gap: 8px; margin-top: auto; }
.psu input {
  flex: 1 1 auto;
  min-width: 0;
  border: 0;
  border-bottom: 1px solid var(--psu-chase);
  background: transparent;
  color: var(--psu-ink);
  font: 500 0.95rem/1.3 "IBM Plex Sans", "Avenir Next", sans-serif;
  padding: 6px 0;
}
.psu button {
  font: 600 0.82rem/1 "IBM Plex Sans", "Avenir Next", sans-serif;
  background: transparent;
  color: var(--psu-ink);
  border: 1px solid var(--psu-chase);
  border-radius: 0;
  padding: 7px 10px;
  cursor: pointer;
}
.psu-plate button { margin-top: auto; align-self: flex-start; }
.psu-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 14px; }
.psu-bar > button { background: var(--psu-chase); color: var(--psu-sheet); }
.psu button:disabled { opacity: 0.45; cursor: not-allowed; }
.psu button:focus-visible, .psu input:focus-visible {
  outline: 2px solid var(--psu-ink);
  outline-offset: 2px;
}
.psu-note { flex: 1 1 16rem; color: var(--psu-muted); line-height: 1.4; }
.psu-error { flex: 1 1 16rem; color: #8e1d2c; line-height: 1.4; }
@media (max-width: 720px) {
  .psu { padding: 28px 16px 32px; }
  .psu h1 { font-size: 1.8rem; }
  .psu-chase { grid-template-columns: 1fr; }
}
`;
