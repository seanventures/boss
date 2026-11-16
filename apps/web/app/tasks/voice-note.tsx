"use client";

import React, { useId, useState } from "react";

/**
 * A voice slip on a task. The person pastes words they already have from a
 * browser or a phone. This screen files those words. It does not place a call
 * and it does not send audio anywhere.
 */

export type VoiceSource = "browser" | "phone";

export type VoiceTranscript = {
  transcriptId: string;
  source: VoiceSource;
  body: string;
  submittedBy: string;
  receiptId: string;
  taskId?: string;
};

export type VoiceAttachInput = {
  transcriptId: string;
  source: VoiceSource;
  body: string;
  submittedBy: string;
};

export type VoiceAttachResult =
  | { created: boolean; transcript: VoiceTranscript }
  | { error: string };

type VoiceNoteProps = {
  taskId: string;
  taskTitle: string;
  orgId: string;
  projectId: string;
  submittedBy: string;
  transcripts?: VoiceTranscript[];
  onAttach?: (input: VoiceAttachInput) => Promise<VoiceAttachResult>;
};

type ApiTranscript = {
  created?: boolean;
  transcript_id: string;
  source: VoiceSource;
  body: string;
  submitted_by: string;
  receipt_id: string;
  task_id?: string;
};

const EMPTY_WORDS = "Paste the transcript. BOSS does not call a speech vendor.";
const NOT_FILED = "The transcript was not filed. Try again.";

export function voicePath(taskId: string): string {
  return `/v1/tasks/${encodeURIComponent(taskId)}/voice`;
}

export async function fileVoiceTranscript(
  taskId: string,
  input: VoiceAttachInput & { orgId: string; projectId: string },
  fetchImpl: typeof fetch = fetch,
): Promise<VoiceAttachResult> {
  const response = await fetchImpl(voicePath(taskId), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      org_id: input.orgId,
      project_id: input.projectId,
      transcript_id: input.transcriptId,
      source: input.source,
      body: input.body,
      submitted_by: input.submittedBy,
    }),
  });
  return readVoiceResult(response);
}

export async function loadVoiceTranscripts(
  taskId: string,
  orgId: string,
  projectId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<VoiceTranscript[]> {
  const query = new URLSearchParams({ org_id: orgId, project_id: projectId });
  const response = await fetchImpl(`${voicePath(taskId)}?${query.toString()}`, {
    headers: { accept: "application/json" },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(messageOf(payload));
  }
  const rows = payload && Array.isArray(payload.transcripts) ? payload.transcripts : [];
  return rows.map(toTranscript);
}

function toTranscript(row: ApiTranscript): VoiceTranscript {
  return {
    transcriptId: row.transcript_id,
    source: row.source,
    body: row.body,
    submittedBy: row.submitted_by,
    receiptId: row.receipt_id,
    ...(row.task_id ? { taskId: row.task_id } : {}),
  };
}

async function readVoiceResult(response: Response): Promise<VoiceAttachResult> {
  const payload = await response.json().catch(() => null);
  if (!response.ok) return { error: messageOf(payload) };
  return { created: Boolean(payload?.created), transcript: toTranscript(payload as ApiTranscript) };
}

function messageOf(payload: unknown): string {
  if (!payload || typeof payload !== "object") return NOT_FILED;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return NOT_FILED;
}

function mergeNote(notes: VoiceTranscript[], next: VoiceTranscript): VoiceTranscript[] {
  if (notes.some((row) => row.transcriptId === next.transcriptId)) return notes;
  return [...notes, next];
}

export function VoiceNote({
  taskId,
  taskTitle,
  orgId,
  projectId,
  submittedBy,
  transcripts = [],
  onAttach,
}: VoiceNoteProps) {
  const baseId = useId();
  const [notes, setNotes] = useState<VoiceTranscript[]>(transcripts);
  const [transcriptId, setTranscriptId] = useState("");
  const [source, setSource] = useState<VoiceSource>("browser");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [replay, setReplay] = useState(false);
  const [pending, setPending] = useState(false);

  async function file(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = transcriptId.trim();
    const words = body.trim();
    if (!id) {
      setReplay(false);
      setError("A transcript id is required.");
      return;
    }
    if (!words) {
      setReplay(false);
      setError(EMPTY_WORDS);
      return;
    }
    setPending(true);
    setError(null);
    setReplay(false);
    const input: VoiceAttachInput = { transcriptId: id, source, body: words, submittedBy };
    try {
      const result = onAttach
        ? await onAttach(input)
        : await fileVoiceTranscript(taskId, { ...input, orgId, projectId });
      if ("error" in result) {
        setError(result.error);
        return;
      }
      const onThisTask = !result.transcript.taskId || result.transcript.taskId === taskId;
      setNotes((current) => (onThisTask ? mergeNote(current, result.transcript) : current));
      setReplay(!result.created);
      if (result.created) {
        setTranscriptId("");
        setBody("");
      }
    } catch {
      setError(NOT_FILED);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="vn" data-testid="voice-note" aria-labelledby={`${baseId}-title`}>
      <style>{VOICE_SLIP_CSS}</style>
      <header className="vn-head">
        <p className="vn-kicker">Voice slip</p>
        <h2 id={`${baseId}-title`} className="vn-title">{taskTitle}</h2>
        <p className="vn-lead">
          BOSS stores the words you paste. It does not place a call, and it does not send audio to a speech vendor.
        </p>
      </header>
      <div className="vn-slip">
        {notes.length === 0 ? (
          <p className="vn-empty" data-testid="voice-empty">Nothing is filed on this task yet.</p>
        ) : (
          <ol className="vn-list">
            {notes.map((note) => (
              <li key={note.transcriptId}>
                <article className="vn-note" data-testid="voice-transcript" data-transcript-id={note.transcriptId}>
                  <div className="vn-rule" aria-hidden="true" data-testid="voice-rule">
                    <span className="vn-hole" />
                    <span className="vn-hole" />
                    <span className="vn-hole" />
                  </div>
                  <div className="vn-copy">
                    <p className="vn-meta">
                      <span className="vn-brass" data-testid="voice-source">{note.source}</span>
                      <span className="vn-id">{note.transcriptId}</span>
                    </p>
                    <p className="vn-body" data-testid="voice-body">{note.body}</p>
                    <p className="vn-by">Filed by {note.submittedBy}.</p>
                    <p className="vn-receipt" data-testid="voice-receipt">Receipt {note.receiptId}</p>
                  </div>
                </article>
              </li>
            ))}
          </ol>
        )}
        <form className="vn-form" onSubmit={file}>
          <fieldset className="vn-sources">
            <legend>Spoken on</legend>
            <div className="vn-tabs" title="Where these words were spoken.">
              <label className="vn-tab">
                <input
                  type="radio"
                  name="source"
                  value="browser"
                  checked={source === "browser"}
                  onChange={() => setSource("browser")}
                />
                Browser
              </label>
              <label className="vn-tab">
                <input
                  type="radio"
                  name="source"
                  value="phone"
                  checked={source === "phone"}
                  onChange={() => setSource("phone")}
                />
                Phone
              </label>
            </div>
          </fieldset>
          <label className="vn-field" htmlFor={`${baseId}-id`}>
            Transcript id
            <input
              id={`${baseId}-id`}
              className="vn-id-input"
              data-testid="voice-id"
              value={transcriptId}
              onChange={(event) => setTranscriptId(event.target.value)}
              autoComplete="off"
              maxLength={200}
            />
          </label>
          <label className="vn-field" htmlFor={`${baseId}-words`}>
            Words
            <textarea
              id={`${baseId}-words`}
              className="vn-words-input"
              data-testid="voice-words"
              value={body}
              onChange={(event) => setBody(event.target.value)}
              rows={5}
              maxLength={20000}
            />
          </label>
          {error ? <p className="vn-error" data-testid="voice-error" role="alert">{error}</p> : null}
          {replay ? (
            <p className="vn-replay" data-testid="voice-replay">Already filed. The first copy stays.</p>
          ) : null}
          <button className="vn-file" data-testid="voice-file" type="submit" disabled={pending}>
            {pending ? "Filing" : "File on task"}
          </button>
        </form>
      </div>
    </section>
  );
}

const VOICE_SLIP_CSS = `
.vn {
  --slip: #E4EFE0;
  --ink: #1C2822;
  --quiet: #4E5C54;
  --cover: #1B2836;
  --cover-ink: #F3EDE4;
  --rule: #D7263D;
  --brass: #E0A106;
  --brass-ink: #1C1404;
  --stamp: #0E6A45;
  --line: #C5D4C4;
  box-sizing: border-box;
  max-width: 38rem;
  padding: 1.15rem 1.15rem 1.3rem;
  background: var(--cover);
  color: var(--cover-ink);
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
}
.vn *, .vn *::before, .vn *::after { box-sizing: border-box; }
.vn-kicker {
  margin: 0;
  font-family: "IBM Plex Mono", "Source Code Pro", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #E0A106;
}
.vn-title {
  margin: 0.2rem 0 0.45rem;
  font-size: 1.85rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.05;
}
.vn-lead, .vn-empty, .vn-body, .vn-by, .vn-error, .vn-replay {
  margin: 0;
  max-width: 36rem;
  line-height: 1.45;
}
.vn-lead { color: #D5DDE4; font-size: 1rem; }
.vn-slip {
  margin-top: 0.9rem;
  background: var(--slip);
  color: var(--ink);
  border-radius: 2px 12px 12px 2px;
  box-shadow: 0 16px 28px rgba(6, 10, 16, 0.35);
}
.vn-list { list-style: none; margin: 0; padding: 0; }
.vn-note {
  display: grid;
  grid-template-columns: 1.4rem 1fr;
  border-bottom: 1px solid var(--line);
}
.vn-rule {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-evenly;
  background: var(--rule);
}
.vn-hole {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 50%;
  background: var(--cover);
}
.vn-copy { padding: 0.75rem 0.9rem 0.8rem 0.75rem; }
.vn-meta {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin: 0 0 0.4rem;
}
.vn-brass {
  display: inline-block;
  padding: 0.08rem 0.4rem 0.12rem;
  background: var(--brass);
  color: var(--brass-ink);
  font-family: "IBM Plex Mono", "Source Code Pro", ui-monospace, monospace;
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  transform: rotate(-2deg);
}
.vn-id, .vn-receipt {
  font-family: "IBM Plex Mono", "Source Code Pro", ui-monospace, monospace;
  font-size: 0.75rem;
  color: var(--quiet);
}
.vn-body { font-size: 1.05rem; }
.vn-by { margin-top: 0.35rem; color: var(--quiet); font-size: 0.92rem; }
.vn-receipt { margin-top: 0.15rem; color: var(--stamp); }
.vn-empty { padding: 0.9rem 1rem 0.2rem 1.15rem; }
.vn-form {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  padding: 0.85rem 1rem 1rem 1.15rem;
}
.vn-sources { border: 0; margin: 0; padding: 0; }
.vn-sources legend {
  padding: 0;
  font-family: "IBM Plex Mono", "Source Code Pro", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--quiet);
}
.vn-tabs { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.35rem; }
.vn-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--brass-ink);
  background: transparent;
  color: var(--ink);
  font: inherit;
}
.vn-tab:has(input:checked) { background: var(--brass); color: var(--brass-ink); }
.vn-field { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.95rem; }
.vn-id-input, .vn-words-input, .vn-file {
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
  color: var(--ink);
}
.vn-id-input, .vn-words-input {
  width: 100%;
  padding: 0.35rem 0;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
}
.vn-words-input { min-height: 6.5rem; resize: vertical; }
.vn-file {
  align-self: flex-start;
  margin-top: 0.2rem;
  padding: 0.45rem 0.85rem;
  background: var(--cover);
  color: var(--cover-ink);
  border: none;
  cursor: pointer;
}
.vn-file:disabled { cursor: progress; opacity: 0.65; }
.vn-error {
  padding: 0.45rem 0.55rem;
  background: #F8D6D2;
  color: #6F1410;
}
.vn input:focus-visible, .vn textarea:focus-visible, .vn button:focus-visible {
  outline: 2px solid var(--rule);
  outline-offset: 2px;
}
@media (max-width: 30rem) {
  .vn { padding: 0.75rem; }
  .vn-title { font-size: 1.4rem; }
  .vn-note { grid-template-columns: 1rem 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  .vn-brass { transform: none; }
}
`;
