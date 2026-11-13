"use client";

/**
 * PROJECT › SETTINGS › TOOLCHAIN (BOSSAPP-825): build this project's own build image, once, by hand.
 * One flow: Build image (a throwaway setup container) → the terminal (install the JDK, Go, browsers,
 * log in to a private tool…) → Capture → a status chip. Once ready, every build of this project runs
 * its commands in that image instead of the default (docker build runtime only — the card says so
 * when the runtime is off). Follows the Setup toolchains row: a pill per state, words in the tip.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Package, Terminal, Trash } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { idGet, idPost, refusalSentence } from "../../lib/identity/api";
import { Ico, Tag } from "./kit";

type Line = { command: string; exit_code: number; stdout?: string; stderr?: string };
type Img = {
  id: string; base: string; status: "building" | "ready" | "failed"; phase: string;
  image_ref: string | null; digest: string | null; error: string | null; commands: number;
  transcript: Line[]; shell_command: string | null; created_at: string;
};
type View = {
  docker_runtime: boolean; registry: string | null; default_image: string | null;
  default_base: string | null; builds_use: string | null; items: Img[];
};

const ROW: React.CSSProperties = { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" };

const q = (projectId: string, actor: string) =>
  `/v1/toolchain-images?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actor)}`;

function chip(img: Img | undefined): { tone: "green" | "amber" | "red" | "idle"; words: string; tip: string } {
  if (!img) return { tone: "idle", words: "Default image", tip: "Builds use the install's default image." };
  if (img.status === "ready") return { tone: "green", words: "Project image ready", tip: `${img.image_ref ?? ""}\n${img.digest ?? ""}` };
  if (img.status === "failed") return { tone: "red", words: img.error?.startsWith("discarded") ? "Discarded" : "Capture failed", tip: img.error ?? "" };
  return img.phase === "capturing"
    ? { tone: "amber", words: "Capturing…", tip: "Committing the setup container. This can take a few minutes." }
    : { tone: "amber", words: "Setup open", tip: `From ${img.base}. Install what builds need, then Capture.` };
}

export function ToolchainImageCard({ projectId, actor }: { projectId: string; actor: string }) {
  const [view, setView] = useState<View | null>(null);
  const [why, setWhy] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cmd, setCmd] = useState("");
  const [out, setOut] = useState<Line[]>([]);
  const [termOpen, setTermOpen] = useState(false);
  const outRef = useRef<HTMLPreElement>(null);

  const load = useCallback(async () => {
    try { setView(await idGet<View>(q(projectId, actor))); } catch (e) { setWhy(refusalSentence(e)); }
  }, [projectId, actor]);
  useEffect(() => { void load(); }, [load]);

  const latest = view?.items[0];
  const open = latest?.status === "building" ? latest : undefined;
  const capturing = open?.phase === "capturing";
  // The chip waits on Capture: poll until it turns ready or failed.
  useEffect(() => {
    if (!capturing) return;
    const t = setInterval(() => void load(), 3000);
    return () => clearInterval(t);
  }, [capturing, load]);
  useEffect(() => { outRef.current?.scrollTo?.({ top: outRef.current.scrollHeight }); }, [out]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setWhy(null);
    try { await fn(); await load(); } catch (e) { setWhy(refusalSentence(e)); } finally { setBusy(false); }
  };
  const body = { project_id: projectId, actor };
  const start = () => act(async () => { await idPost("/v1/toolchain-images", body); setOut([]); setTermOpen(true); });
  const run = () => {
    const c = cmd.trim();
    if (!c || !open) return;
    void act(async () => {
      const r = await idPost<Line>(`/v1/toolchain-images/${encodeURIComponent(open.id)}/exec`, { ...body, command: c });
      setOut((o) => [...o, r]); setCmd("");
    });
  };
  const capture = () => open && act(() => idPost(`/v1/toolchain-images/${encodeURIComponent(open.id)}/capture`, body));
  const discard = () => open && act(() => idPost(`/v1/toolchain-images/${encodeURIComponent(open.id)}/discard`, body));

  const ready = view?.items.find((i) => i.status === "ready");
  const c = chip(open ?? latest);
  const shown = out.length ? out : (open?.transcript ?? []);
  return (
    <section className="px-card" aria-labelledby="px-set-toolchain" data-testid="px-set-toolchain" style={{ scrollMarginTop: 80 }}>
      <div className="px-card-h">
        <Ico icon={Package} c="teal" />
        <h2 id="px-set-toolchain">Toolchain</h2>
        <span className="px-spacer" />
        <span data-testid="px-toolchain-chip" data-state={open ? open.phase : latest?.status ?? "default"}><Tag tone={c.tone} title={c.tip}>{c.words}</Tag></span>
      </div>
      {!view ? <p className="px-muted">{why ?? "Loading…"}</p> : (
        <div style={{ display: "grid", gap: 8 }}>
          <small className="px-muted" title={view.builds_use ?? ""}>
            Builds run in <code className="px-code">{(ready && view.builds_use === ready.image_ref) ? "this project's image" : (view.builds_use ?? "the default image")}</code>
          </small>
          {!view.docker_runtime ? (
            <small className="px-muted" data-testid="px-toolchain-off">
              Project images need the docker build runtime. Set <code className="px-code">BOSS_SANDBOX_COMMAND_RUNTIME=docker</code> on the BOSS host.
            </small>
          ) : !open ? (
            <div style={ROW}>
              <GatedButton permission="host:manage" projectId={projectId} className="px-chip px-linkbtn" data-testid="px-toolchain-build"
                           disabled={busy} onClick={() => void start()} title={`Opens a setup container from ${view.default_base ?? "the default image"}`}>
                <Terminal size={14} aria-hidden /> {ready ? "Rebuild image" : "Build image"}
              </GatedButton>
            </div>
          ) : capturing ? null : (
            <>
              <div style={ROW}>
                <button type="button" className="px-chip px-linkbtn" data-testid="px-toolchain-terminal" onClick={() => setTermOpen((v) => !v)}>
                  <Terminal size={14} aria-hidden /> {termOpen ? "Hide terminal" : "Open terminal"}
                </button>
                <GatedButton permission="host:manage" projectId={projectId} className="px-chip px-linkbtn" data-testid="px-toolchain-capture"
                             disabled={busy} onClick={() => void capture()} title="Save this container as the project's build image">
                  <Camera size={14} aria-hidden /> Capture
                </GatedButton>
                <GatedButton permission="host:manage" projectId={projectId} className="px-chip px-linkbtn" data-testid="px-toolchain-discard"
                             disabled={busy} onClick={() => void discard()} title="Close the setup container without an image">
                  <Trash size={14} aria-hidden /> Discard
                </GatedButton>
              </div>
              {termOpen ? (
                <div data-testid="px-toolchain-term">
                  <pre ref={outRef} className="px-mono" style={{ maxHeight: 260, overflow: "auto", background: "var(--px-code-bg, #0f172a)", color: "#e2e8f0", padding: 10, borderRadius: 8, fontSize: 12, whiteSpace: "pre-wrap", margin: 0 }}>
                    {shown.length ? shown.map((l, i) => `$ ${l.command}\n${l.stdout ?? ""}${l.stderr ?? ""}${l.exit_code ? `[exit ${l.exit_code}]\n` : ""}`).join("")
                      : `# Setup container from ${open.base}, as root, with network.\n# Install system-wide (builds run as a non-root user). Anything typed here, logins included, is saved in the image.\n`}
                  </pre>
                  <form onSubmit={(e) => { e.preventDefault(); run(); }} style={{ ...ROW, marginTop: 6 }}>
                    <input aria-label="Command" className="px-mono" style={{ flex: 1, minWidth: 0 }} value={cmd} disabled={busy}
                           placeholder="apt-get update && apt-get install -y openjdk-21-jdk" onChange={(e) => setCmd(e.target.value)} data-testid="px-toolchain-cmd" />
                    <button type="submit" className="px-chip px-linkbtn" disabled={busy || !cmd.trim()} data-testid="px-toolchain-run">Run</button>
                  </form>
                  {open.shell_command ? <small className="px-muted">On the BOSS host: <code className="px-code">{open.shell_command}</code></small> : null}
                </div>
              ) : null}
            </>
          )}
          {why ? <small role="alert" data-testid="px-toolchain-why">{why}</small> : null}
        </div>
      )}
    </section>
  );
}
