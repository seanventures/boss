"use client";

/**
 * Proof bench (BOSSAPP-1245). A named draft keeps its own chat and its own
 * cropped proof. The live URL and the commit list are props: this screen
 * never writes them.
 */
import React, { useState } from "react";

const DRAFT_HOST = "https://draft.boss.test";

export type DraftLine = { role: "operator"; text: string };

export type OpenDraft = {
  name: string;
  slug: string;
  chatId: string;
  previewUrl: string;
  messages: DraftLine[];
};

export function slugDraftName(raw: string): string | null {
  const slug = raw.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (!slug) return null;
  const cut = slug.slice(0, 80).replace(/-+$/g, "");
  return cut || null;
}

export function draftPreviewUrl(appId: string, slug: string, publishedUrl: string): string {
  const base = `${DRAFT_HOST}/${appId}/${slug}`;
  if (base === publishedUrl.trim().replace(/\/$/, "")) return `${base}/proof`;
  return base;
}

export function NamedDraft({
  appId = "app",
  publishedUrl = "",
  commits = [],
}: {
  appId?: string;
  publishedUrl?: string;
  commits?: readonly string[];
}) {
  const [name, setName] = useState("");
  const [line, setLine] = useState("");
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, OpenDraft>>({});
  const [current, setCurrent] = useState<string | null>(null);
  const draft = current ? drafts[current] : undefined;
  const commitList = commits.join("\n");

  function openDraft() {
    const slug = slugDraftName(name);
    if (!slug) {
      setError("Name the draft.");
      return;
    }
    setError("");
    setDrafts((prev) => {
      if (prev[slug]) return prev;
      return {
        ...prev,
        [slug]: {
          name: name.trim(),
          slug,
          chatId: `chat_${appId}_${slug}`,
          previewUrl: draftPreviewUrl(appId, slug, publishedUrl),
          messages: [],
        },
      };
    });
    setCurrent(slug);
  }

  function sendLine() {
    if (!current) {
      setError("Open the draft.");
      return;
    }
    const text = line.trim();
    if (!text) {
      setError("Write a line.");
      return;
    }
    const slug = current;
    setError("");
    setLine("");
    setDrafts((prev) => {
      const found = prev[slug];
      if (!found) return prev;
      return {
        ...prev,
        [slug]: { ...found, messages: [...found.messages, { role: "operator", text }] },
      };
    });
  }

  return (
    <div className="nd-bench">
      <style>{BENCH_CSS}</style>
      <header className="nd-live">
        <RegistrationMark />
        <div className="nd-live-copy">
          <p className="nd-kicker">Live URL</p>
          <p className="nd-url" data-testid="named-draft-published-url" title="Live address. Unchanged by a draft.">{publishedUrl || "No published URL yet."}</p>
        </div>
        <div data-testid="named-draft-commits" data-commits={commitList} title="No commit is added.">
          <p className="nd-kicker">Commits</p>
          <p className="nd-mono">{commits.length ? commits.join(" ") : "None"}</p>
        </div>
        <p className="nd-off" data-testid="named-draft-git-sync" title="Stays off git. No commit.">Git sync off</p>
        <p className="nd-off" data-testid="named-draft-commit-flag">No commit</p>
      </header>

      <div className="nd-lead">
        <p className="nd-kicker">Proof bench</p>
        <h1>Named draft</h1>
        <p>This draft has its own chat and its own preview.</p>
        <p>Opening it writes no commit.</p>
        <p>The published URL does not change.</p>
      </div>

      <div className="nd-split">
        <section className="nd-ticket" aria-label="Draft">
          <label className="nd-kicker" htmlFor="nd-name">Draft name</label>
          <div className="nd-open">
            <input
              id="nd-name"
              data-testid="named-draft-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="evening-copy"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={error === "Name the draft."}
            />
            <button type="button" data-testid="named-draft-open" onClick={openDraft} title="Open this named draft.">
              Open draft
            </button>
          </div>
          {error ? <p className="nd-error" role="alert" data-testid="named-draft-error">{error}</p> : null}
          {Object.keys(drafts).length > 0 ? (
            <div className="nd-tabs" role="tablist" aria-label="Open drafts">
              {Object.values(drafts).map((item) => (
                <button
                  key={item.slug}
                  type="button"
                  role="tab"
                  aria-pressed={item.slug === current}
                  data-testid={`named-draft-tab-${item.slug}`}
                  onClick={() => { setError(""); setCurrent(item.slug); }}
                >
                  {item.name}
                </button>
              ))}
            </div>
          ) : null}
          {draft ? (
            <section data-testid="named-draft-chat" data-chat-id={draft.chatId} title="Lines stay on this draft.">
              <h2>Chat</h2>
              <p className="nd-nameplate">{draft.name}</p>
              {draft.messages.length === 0 ? <p>Write the first line.</p> : null}
              <div data-testid="named-draft-transcript">{draft.messages.map((message, index) => <p key={`${draft.slug}-${index}`}>{message.text}</p>)}</div>
              <div className="nd-open">
                <input
                  data-testid="named-draft-message"
                  value={line}
                  onChange={(event) => setLine(event.target.value)}
                  placeholder="A line for this draft"
                  autoComplete="off"
                  aria-label="Chat line"
                />
                <button type="button" data-testid="named-draft-send" onClick={sendLine} title="Add a line to this chat.">
                  Send
                </button>
              </div>
            </section>
          ) : null}
        </section>

        <section className="nd-sheet" aria-label="Preview">
          {draft ? (
            <figure
              className="nd-proof"
              data-testid="named-draft-preview"
              data-preview-url={draft.previewUrl}
              title="Not the published URL."
            >
              <span className="nd-crop nd-crop-nw" aria-hidden="true" />
              <span className="nd-crop nd-crop-ne" aria-hidden="true" />
              <span className="nd-crop nd-crop-sw" aria-hidden="true" />
              <span className="nd-crop nd-crop-se" aria-hidden="true" />
              <h2>Preview</h2>
              <p className="nd-url" data-testid="named-draft-preview-url">{draft.previewUrl}</p>
              <p>Not the live URL.</p>
            </figure>
          ) : (
            <p>Open a draft to see its proof.</p>
          )}
        </section>
      </div>
    </div>
  );
}

function RegistrationMark() {
  return (
    <svg className="nd-mark" viewBox="0 0 48 48" aria-hidden="true">
      <circle cx="24" cy="24" r="14" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M24 4v40M4 24h40" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="24" cy="24" r="2.5" fill="currentColor" />
    </svg>
  );
}

const BENCH_CSS = `
.nd-bench {
  box-sizing: border-box;
  min-height: 100%;
  padding: 28px 32px 48px;
  background: #E4EEF2;
  color: #163A4A;
  font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
  font-size: 16px;
  line-height: 1.45;
}
.nd-bench * { box-sizing: border-box; }
.nd-bench h1, .nd-bench h2, .nd-bench p { margin: 0; }
.nd-bench h1 {
  font-family: "Franklin Gothic Condensed", "Arial Narrow", "Nimbus Sans Narrow", sans-serif;
  font-size: 40px;
  font-weight: 700;
  letter-spacing: 0.04em;
  line-height: 0.95;
  text-transform: uppercase;
}
.nd-bench h2 {
  font-family: "Franklin Gothic Condensed", "Arial Narrow", "Nimbus Sans Narrow", sans-serif;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.nd-kicker {
  font-family: "Franklin Gothic Condensed", "Arial Narrow", "Nimbus Sans Narrow", sans-serif;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #1F6F8B;
}
.nd-live {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 18px 28px;
  padding-bottom: 18px;
  border-bottom: 1.5px solid #1F6F8B;
}
.nd-live-copy { min-width: 0; flex: 1 1 240px; }
.nd-mark { width: 42px; height: 42px; color: #C4552A; flex: none; }
.nd-url, .nd-mono {
  font-family: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 14px;
  overflow-wrap: anywhere;
}
.nd-off {
  margin: 0;
  color: #7A2418;
  font-family: "Franklin Gothic Condensed", "Arial Narrow", "Nimbus Sans Narrow", sans-serif;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.nd-lead { max-width: 38rem; padding: 22px 0 8px; }
.nd-lead p { margin-top: 8px; }
.nd-split {
  display: grid;
  grid-template-columns: minmax(260px, 0.9fr) minmax(280px, 1.1fr);
  gap: 28px;
  align-items: start;
  margin-top: 22px;
}
.nd-ticket, .nd-sheet { min-width: 0; }
.nd-open { display: flex; gap: 8px; margin-top: 8px; }
.nd-open input {
  flex: 1 1 auto;
  min-width: 0;
  border: 1.5px solid #163A4A;
  background: #F7FBFC;
  color: #163A4A;
  font: inherit;
  padding: 10px 12px;
}
.nd-bench button {
  border: 1.5px solid #163A4A;
  background: #163A4A;
  color: #F7FBFC;
  font-family: "Franklin Gothic Condensed", "Arial Narrow", "Nimbus Sans Narrow", sans-serif;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 10px 14px;
  cursor: pointer;
}
.nd-bench button[aria-pressed="true"] { background: #1F6F8B; border-color: #1F6F8B; }
.nd-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.nd-error { margin-top: 8px; color: #7A2418; }
.nd-nameplate {
  margin: 10px 0;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, serif;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: 0.02em;
  animation: nd-stamp 420ms ease-out both;
}
.nd-proof {
  position: relative;
  min-height: 280px;
  margin: 0;
  padding: 36px 28px;
  background: #F7FBFC;
}
.nd-crop {
  position: absolute;
  width: 16px;
  height: 16px;
  border-color: #1F6F8B;
  border-style: solid;
}
.nd-crop-nw { top: 10px; left: 10px; border-width: 1.5px 0 0 1.5px; }
.nd-crop-ne { top: 10px; right: 10px; border-width: 1.5px 1.5px 0 0; }
.nd-crop-sw { bottom: 10px; left: 10px; border-width: 0 0 1.5px 1.5px; }
.nd-crop-se { bottom: 10px; right: 10px; border-width: 0 1.5px 1.5px 0; }
.nd-proof .nd-url { margin: 16px 0 10px; }
.nd-bench :focus-visible {
  outline: 2px solid #1F6F8B;
  outline-offset: 2px;
}
@media (max-width: 720px) {
  .nd-bench { padding: 20px 16px 36px; }
  .nd-split { grid-template-columns: 1fr; }
  .nd-bench h1 { font-size: 32px; }
}
@media (prefers-reduced-motion: reduce) {
  .nd-nameplate { animation: none; }
}
@keyframes nd-stamp {
  from { opacity: 0; letter-spacing: 0.18em; }
  to { opacity: 1; letter-spacing: 0.02em; }
}
`;
