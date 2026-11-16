"use client";

/**
 * Shared apply board (BOSSAPP-1252). Copies hang off a steel rail.
 * The plate is the shared head. It moves only when a finished copy is applied.
 */
import React, { useEffect, useRef, useState } from "react";

import { idGet, idPost, refusalSentence } from "../../lib/identity/api";

const UNFINISHED =
  "This copy is not finished. Finish it before it can touch the shared branch.";

export type ApplyBoardProps = {
  projectId: string;
  boardId?: string | null;
  actor?: string;
};

type Board = {
  id: string;
  project_id: string;
  shared_branch: string;
  shared_head: string;
};

type Copy = {
  id: string;
  task_key: string;
  title: string;
  teammate: string;
  copy_branch: string;
  base_head: string;
  copy_head: string;
  status: "running" | "finished" | "applied" | "dead" | string;
  note: string;
  failure_reason: string;
  failure_kind: string;
  replayed?: boolean;
};

type View = { board: Board; copies?: Copy[]; copy?: Copy | null };

const STYLES = `
.apply-board {
  --ab-cloth: #d7e4e2;
  --ab-ink: #17262b;
  --ab-slip: #f3f6f5;
  --ab-carbon: #0e6a66;
  --ab-stamp: #c4492c;
  --ab-brass: #8a5610;
  --ab-steel: #1c3338;
  --ab-plate: #e7f2f0;
  --ab-dead: #8e2f2f;
  --ab-rule: #b7c9c6;
  --ab-display: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  --ab-body: "Avenir Next", "Segoe UI", sans-serif;
  --ab-mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  box-sizing: border-box;
  min-height: 100%;
  padding: 28px 22px 48px;
  background: var(--ab-cloth);
  color: var(--ab-ink);
  font-family: var(--ab-body);
  font-size: 16px;
  line-height: 1.45;
}
.apply-board *, .apply-board *::before, .apply-board *::after { box-sizing: border-box; }
.apply-board h2, .apply-board .ab-kicker, .apply-board .ab-stamp, .apply-board .ab-status {
  font-family: var(--ab-display);
}
.apply-board h2 {
  margin: 0;
  font-size: 2.4rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  line-height: 0.95;
  text-transform: uppercase;
}
.apply-board .ab-kicker {
  margin: 0 0 6px;
  color: var(--ab-carbon);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.apply-board .ab-rail { max-width: 760px; margin: 0 auto 22px; }
.apply-board .ab-plate {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px 24px;
  align-items: baseline;
  margin-top: 14px;
  padding: 18px 20px 16px;
  background: var(--ab-steel);
  color: var(--ab-plate);
  box-shadow: inset 0 -8px 0 rgba(0, 0, 0, 0.18);
}
.apply-board .ab-plate[data-moved="true"] { animation: ab-ink 700ms ease; }
@keyframes ab-ink {
  from { background: var(--ab-stamp); }
  to { background: var(--ab-steel); }
}
.apply-board .ab-branch, .apply-board .ab-sha {
  font-family: var(--ab-mono);
  letter-spacing: 0.08em;
}
.apply-board .ab-sha { font-size: 1.05rem; }
.apply-board .ab-label {
  display: block;
  margin-bottom: 2px;
  color: #9eb8b4;
  font-family: var(--ab-display);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.apply-board .ab-stack, .apply-board .ab-open, .apply-board .ab-start {
  max-width: 760px;
  margin: 0 auto;
}
.apply-board .ab-slips { display: flex; flex-direction: column; }
.apply-board .ab-slip {
  position: relative;
  margin-top: -10px;
  padding: 18px 18px 16px 22px;
  background: var(--ab-slip);
  border: 1px solid var(--ab-rule);
  border-left: 12px solid var(--ab-carbon);
  box-shadow: 0 10px 0 rgba(23, 38, 43, 0.03);
}
.apply-board .ab-slip:first-child { margin-top: 0; }
.apply-board .ab-slip[data-status="finished"] { border-left-color: var(--ab-brass); }
.apply-board .ab-slip[data-status="applied"] { border-left-color: var(--ab-stamp); }
.apply-board .ab-slip[data-status="dead"] { border-left-color: var(--ab-dead); }
.apply-board .ab-who {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  align-items: baseline;
  margin: 0;
}
.apply-board .ab-who strong { font-family: var(--ab-display); font-size: 1.35rem; letter-spacing: 0.03em; }
.apply-board .ab-task { font-family: var(--ab-mono); font-size: 0.85rem; color: var(--ab-carbon); }
.apply-board .ab-status {
  margin-left: auto;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.apply-board .ab-title { margin: 6px 0 0; font-size: 1.05rem; }
.apply-board .ab-meta, .apply-board .ab-note {
  margin: 6px 0 0;
  color: #3d5258;
  font-family: var(--ab-mono);
  font-size: 0.82rem;
}
.apply-board .ab-rule-copy, .apply-board .ab-dead-copy, .apply-board .ab-refusal, .apply-board .ab-empty {
  max-width: 62ch;
}
.apply-board .ab-rule-copy { color: var(--ab-brass); }
.apply-board .ab-dead-copy, .apply-board .ab-refusal { color: var(--ab-dead); }
.apply-board .ab-stamp {
  position: absolute;
  top: 16px;
  right: 16px;
  padding: 4px 8px;
  border: 3px solid var(--ab-stamp);
  color: var(--ab-stamp);
  font-size: 0.95rem;
  font-weight: 700;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  transform: rotate(-8deg);
  pointer-events: none;
}
.apply-board form { display: flex; flex-wrap: wrap; gap: 10px 12px; align-items: end; margin-top: 12px; }
.apply-board label { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 0.78rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
.apply-board input {
  min-width: 0;
  height: 36px;
  padding: 0 10px;
  border: 1px solid var(--ab-rule);
  background: #fff;
  color: var(--ab-ink);
  font-family: var(--ab-mono);
  font-size: 0.9rem;
  letter-spacing: 0;
  text-transform: none;
}
.apply-board button {
  height: 36px;
  padding: 0 14px;
  border: 1px solid var(--ab-steel);
  background: var(--ab-steel);
  color: var(--ab-plate);
  font-family: var(--ab-display);
  font-size: 0.95rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  cursor: pointer;
}
.apply-board button.ab-quiet { background: transparent; color: var(--ab-ink); }
.apply-board button:focus-visible, .apply-board input:focus-visible {
  outline: 2px solid var(--ab-carbon);
  outline-offset: 2px;
}
.apply-board button:disabled { opacity: 0.55; cursor: not-allowed; }
.apply-board .ab-open, .apply-board .ab-start {
  margin-top: 18px;
  padding-top: 8px;
}
@media (max-width: 640px) {
  .apply-board { padding: 18px 12px 36px; }
  .apply-board h2 { font-size: 1.8rem; }
  .apply-board .ab-stamp { position: static; display: inline-block; margin-top: 8px; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .apply-board .ab-plate[data-moved="true"] { animation: none; }
}
`;

function mergeCopy(copies: Copy[], copy: Copy): Copy[] {
  const next = copies.map((item) => (item.id === copy.id ? { ...item, ...copy } : item));
  return next.some((item) => item.id === copy.id) ? next : [...copies, copy];
}

function statusWord(status: string): string {
  if (status === "running") return "Running";
  if (status === "finished") return "Finished";
  if (status === "applied") return "Applied";
  if (status === "dead") return "Dead letter";
  return status;
}

export function ApplyBoard({ projectId, boardId = null, actor = "operator" }: ApplyBoardProps) {
  const [board, setBoard] = useState<Board | null>(null);
  const [copies, setCopies] = useState<Copy[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [refusal, setRefusal] = useState("");
  const [busy, setBusy] = useState(false);
  const [branch, setBranch] = useState("main");
  const [head, setHead] = useState("");
  const [taskKey, setTaskKey] = useState("");
  const [title, setTitle] = useState("");
  const [teammate, setTeammate] = useState("");
  const seenHead = useRef<string | null>(null);
  const moved = Boolean(board && seenHead.current && seenHead.current !== board.shared_head);

  useEffect(() => {
    if (board) seenHead.current = board.shared_head;
  }, [board]);

  useEffect(() => {
    if (!projectId || !boardId) return;
    let cancelled = false;
    setLoading(true);
    const path = `/v1/projects/${encodeURIComponent(projectId)}/task-copy-boards/${encodeURIComponent(boardId)}`;
    idGet<View>(path).then((view) => {
      if (cancelled) return;
      setBoard(view.board);
      setCopies(view.copies ?? []);
      setLoadError("");
    }).catch((error: unknown) => {
      if (!cancelled) setLoadError(refusalSentence(error));
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [projectId, boardId]);

  async function send(path: string, body: object) {
    setBusy(true);
    setRefusal("");
    try {
      const answer = await idPost<View>(path, { actor, ...body });
      setBoard(answer.board);
      if (answer.copies) setCopies(answer.copies);
      if (answer.copy) setCopies((items) => mergeCopy(items, answer.copy as Copy));
      return answer;
    } catch (error) {
      setRefusal(refusalSentence(error));
      return null;
    } finally {
      setBusy(false);
    }
  }

  const root = `/v1/projects/${encodeURIComponent(projectId)}`;

  return (
    <section className="apply-board" data-testid="apply-board">
      <style>{STYLES}</style>
      <header className="ab-rail">
        <p className="ab-kicker">Copies stay off this rail</p>
        <h2>Apply board</h2>
        {board ? (
          <div className="ab-plate" data-testid="shared-plate" data-moved={moved ? "true" : "false"}>
            <div>
              <span className="ab-label">Branch</span>
              <span className="ab-branch" data-testid="shared-branch">{board.shared_branch}</span>
            </div>
            <div>
              <span className="ab-label">Head</span>
              <span className="ab-sha" data-testid="shared-head">{board.shared_head}</span>
            </div>
          </div>
        ) : null}
      </header>

      {!projectId ? <p className="ab-empty">Open a project first.</p> : null}
      {loading ? <p className="ab-empty">Loading the board.</p> : null}
      {loadError ? <p className="ab-refusal" role="alert">{loadError}</p> : null}
      {refusal ? <p className="ab-refusal" data-testid="apply-refusal" role="alert">{refusal}</p> : null}

      {projectId && !boardId && !board ? (
        <form className="ab-open" onSubmit={(event) => {
          event.preventDefault();
          void send(`${root}/task-copy-boards`, { shared_branch: branch, shared_head: head });
        }}>
          <label>
            Shared branch
            <input name="shared_branch" aria-label="Shared branch" value={branch}
                   onChange={(event) => setBranch(event.target.value)} />
          </label>
          <label>
            Starting head
            <input name="shared_head" aria-label="Starting head" value={head}
                   onChange={(event) => setHead(event.target.value)} />
          </label>
          <button type="submit" data-testid="open-board" disabled={busy}
                  title="Open the shared branch">
            Open board
          </button>
        </form>
      ) : null}

      {board ? (
        <div className="ab-stack">
          {copies.length === 0 ? (
            <p className="ab-empty">No copies yet. Start one for a teammate. The shared branch stays put.</p>
          ) : (
            <div className="ab-slips">
              {copies.map((copy) => (
                <article key={copy.id} className="ab-slip" data-testid={`copy-${copy.id}`} data-status={copy.status}>
                  {copy.status === "applied" ? <span className="ab-stamp" aria-hidden>Applied</span> : null}
                  <p className="ab-who">
                    <strong>{copy.teammate}</strong>
                    <span className="ab-task">{copy.task_key}</span>
                    <span className="ab-status" data-testid={`copy-status-${copy.id}`}>{statusWord(copy.status)}</span>
                  </p>
                  <p className="ab-title">{copy.title}</p>
                  <p className="ab-meta">{copy.copy_branch}</p>
                  <p className="ab-note" data-testid={`copy-head-${copy.id}`}>{copy.copy_head}</p>
                  {copy.note ? <p className="ab-note">{copy.note}</p> : null}
                  {copy.status === "running" ? (
                    <p className="ab-rule-copy" data-testid={`unfinished-${copy.id}`}>{UNFINISHED}</p>
                  ) : null}
                  {copy.status === "dead" ? (
                    <p className="ab-dead-copy" data-testid={`dead-letter-${copy.id}`}>{copy.failure_reason}</p>
                  ) : null}
                  {copy.status === "running" ? (
                    <SlipWork copy={copy} busy={busy} onWork={(note, copyHead) => send(
                      `${root}/task-copies/${encodeURIComponent(copy.id)}/work`,
                      { note, copy_head: copyHead })}
                      onFinish={() => send(`${root}/task-copies/${encodeURIComponent(copy.id)}/finish`, {})}
                      onFail={(reason) => send(`${root}/task-copies/${encodeURIComponent(copy.id)}/fail`, { reason })} />
                  ) : null}
                  {copy.status === "finished" ? (
                    <form onSubmit={(event) => {
                      event.preventDefault();
                      void send(`${root}/task-copies/${encodeURIComponent(copy.id)}/apply`, {});
                    }}>
                      <button type="submit" data-testid={`apply-${copy.id}`} disabled={busy}
                              title="Stamp this copy onto the shared branch">
                        Apply
                      </button>
                    </form>
                  ) : null}
                  {copy.status === "dead" && copy.failure_kind === "run" ? (
                    <form onSubmit={(event) => {
                      event.preventDefault();
                      void send(`${root}/task-copies/${encodeURIComponent(copy.id)}/retry`, {});
                    }}>
                      <button type="submit" data-testid={`retry-${copy.id}`} disabled={busy}
                              title="Run the copy again">
                        Retry
                      </button>
                    </form>
                  ) : null}
                  {copy.status === "dead" && copy.failure_kind === "conflict" ? (
                    <RebaseSlip copy={copy} busy={busy} onRebase={(copyHead) => send(
                      `${root}/task-copies/${encodeURIComponent(copy.id)}/rebase`,
                      { copy_head: copyHead })} />
                  ) : null}
                </article>
              ))}
            </div>
          )}
          <form className="ab-start" onSubmit={(event) => {
            event.preventDefault();
            void send(`${root}/task-copies`, {
              board_id: board.id, task_key: taskKey, title, teammate,
            });
          }}>
            <label>
              Task
              <input name="task_key" aria-label="Task" value={taskKey}
                     onChange={(event) => setTaskKey(event.target.value)} />
            </label>
            <label>
              Title
              <input name="title" aria-label="Title" value={title}
                     onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label>
              Teammate
              <input name="teammate" aria-label="Teammate" value={teammate}
                     onChange={(event) => setTeammate(event.target.value)} />
            </label>
            <button type="submit" data-testid="start-copy" disabled={busy}
                    title="Open an isolated copy">
              Start copy
            </button>
          </form>
        </div>
      ) : null}
    </section>
  );
}

function SlipWork({ copy, busy, onWork, onFinish, onFail }: {
  copy: Copy;
  busy: boolean;
  onWork: (note: string, copyHead: string) => void;
  onFinish: () => void;
  onFail: (reason: string) => void;
}) {
  const [note, setNote] = useState(copy.note);
  const [copyHead, setCopyHead] = useState(copy.copy_head);
  const [reason, setReason] = useState("");
  return (
    <>
      <form onSubmit={(event) => { event.preventDefault(); onWork(note, copyHead); }}>
        <label>
          Note
          <input aria-label={`Note for ${copy.teammate}`} value={note}
                 onChange={(event) => setNote(event.target.value)} />
        </label>
        <label>
          Copy head
          <input aria-label={`Copy head for ${copy.teammate}`} value={copyHead}
                 onChange={(event) => setCopyHead(event.target.value)} />
        </label>
        <button type="submit" disabled={busy} title="Save work on the copy">Save work</button>
        <button type="button" className="ab-quiet" disabled={busy} title="Mark the copy finished"
                data-testid={`finish-${copy.id}`} onClick={onFinish}>
          Finish
        </button>
      </form>
      <form onSubmit={(event) => { event.preventDefault(); onFail(reason); }}>
        <label>
          Failure
          <input aria-label={`Failure for ${copy.teammate}`} value={reason}
                 onChange={(event) => setReason(event.target.value)} />
        </label>
        <button type="submit" className="ab-quiet" disabled={busy} title="Park a failed run">
          Mark failed
        </button>
      </form>
    </>
  );
}

function RebaseSlip({ copy, busy, onRebase }: {
  copy: Copy;
  busy: boolean;
  onRebase: (copyHead: string) => void;
}) {
  const [copyHead, setCopyHead] = useState("");
  return (
    <form onSubmit={(event) => { event.preventDefault(); onRebase(copyHead); }}>
      <label>
        Rebased head
        <input aria-label={`Rebased head for ${copy.teammate}`} data-testid={`rebase-head-${copy.id}`}
               value={copyHead} onChange={(event) => setCopyHead(event.target.value)} />
      </label>
      <button type="submit" data-testid={`rebase-${copy.id}`} disabled={busy}
              title="Set the copy onto the current head">
        Rebase
      </button>
    </form>
  );
}
