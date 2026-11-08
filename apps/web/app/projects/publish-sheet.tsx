"use client";

/**
 * PUBLISH TO GITHUB (BOSSAPP-921): a local project goes to GitHub whole — its history, its open
 * changes as pull requests — and from then on builds there. One sheet: owner, repo name (a new
 * repo, or an existing EMPTY one), private, then Publish. Progress is a small diagram: create
 * repo → push history → open PRs → switch. A refusal (the repo has other history, no permission,
 * a personal account) is said in the engine's words with its one fix; pressing again picks up
 * where it stopped (the engine's publish is resumable).
 *
 * Opened from the project header's quiet chip ("Local · Publish to GitHub"), from Settings › Code,
 * and by `?publish=owner/repo` (Add a project lands here when GitHub refused the new repo).
 */
import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowRight, Check, CircleNotch, CloudArrowUp, GithubLogo, Hand, HardDrives, LockKey, X } from "@phosphor-icons/react";

import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError } from "../../lib/identity/api";
import { addProjectApi, repoCheckFix, repoSlug, type RepoCheck } from "../../lib/add-project";
import { accountsOf } from "../../lib/start-project";
import { PUBLISH_STEPS, localFirstApi, publishSteps, type LocalRepo, type PublishRefusal, type PublishResult } from "../../lib/local-first";
import { useRegistry } from "../start-project/import-repo";
import "./publish.css";

export function PublishSheet({ projectId, local, prefill = "", onClose, onDone }: {
  projectId: string; local: LocalRepo | null;
  /** "owner/repo" to start from (Add a project's refused new repo). */
  prefill?: string;
  onClose: () => void; onDone: (result: PublishResult) => void;
}) {
  const titleId = useId();
  const guarded = useGuardedCall();
  const { registry } = useRegistry(projectId);
  const accounts = useMemo(() => accountsOf(registry), [registry]);
  const [pOwner, pRepo] = prefill.includes("/") ? prefill.split("/", 2) : ["", ""];
  const localName = (local?.full_name ?? "").split("/")[1] ?? "";
  const [owner, setOwner] = useState(pOwner);
  const [repo, setRepo] = useState(pRepo || repoSlug(localName || "app"));
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [priv, setPriv] = useState(true);
  useEffect(() => { if (!owner && accounts.length) setOwner(accounts[0]); }, [accounts, owner]);
  // Can BOSS make it (or publish into it)? Asked as the person types, in the engine's words.
  const [check, setCheck] = useState<RepoCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [askAgain, setAskAgain] = useState(0);
  useEffect(() => {
    if (!owner || !repo) { setCheck(null); return undefined; }
    const ctl = new AbortController();
    setChecking(true);
    const ask = (left: number) => addProjectApi.checkRepo(projectId, owner, repo, mode === "new" ? "create" : "existing", ctl.signal)
      .then((c) => { setCheck(c); setChecking(false); })
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        // Asked once more on its own (a read refused while the page still loads who you are);
        // then said, with Ask again.
        if (left > 0) { window.setTimeout(() => { if (!ctl.signal.aborted) void ask(left - 1); }, 1200); return; }
        setCheck({ ok: false, full_name: `${owner}/${repo}`, reason: "failed", exists: false, owner_type: null,
                   words: `Couldn’t ask GitHub now (${e instanceof Error ? e.message : String(e)}).` });
        setChecking(false);
      });
    const t = window.setTimeout(() => { void ask(1); }, 400);
    return () => { window.clearTimeout(t); ctl.abort(); };
  }, [projectId, owner, repo, mode, askAgain]);
  // The repo already exists: publishing into it as an existing (empty) one is the other choice.
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PublishResult | null>(null);
  const [refusal, setRefusal] = useState<PublishRefusal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const steps = publishSteps({ busy, create: mode === "new", result, refusal, openChanges: local?.open_change_sets ?? null });
  const fix = repoCheckFix(refusal ? { ok: false, full_name: `${owner}/${repo}`, reason: refusal.error } : check);
  const blocked = !owner ? "Pick the owner" : !repo ? "Name the repo" : check && !check.ok && check.full_name === `${owner}/${repo}` && !refusal ? check.words : "";
  const press = async () => {
    if (busy || blocked) return;
    setBusy(true); setError(null); setRefusal(null);
    try {
      const r = await guarded("Publish this project to GitHub.", () => localFirstApi.publish(projectId, { owner, repo, private: priv, create: mode === "new" }));
      if (r.ok) { setResult(r.data); onDone(r.data); } else setRefusal(r.refusal);
    } catch (e) {
      setError(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not published: “Confirm it’s you” was closed."
        : e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy && !e.defaultPrevented && !document.querySelector("[data-testid='step-up-sheet']")) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  const done = !!result;
  const sheet = (
    <div className="pub-wrap" data-testid="publish-backdrop" onPointerDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="pub-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid="publish-sheet" ref={box}>
        <div className="pub-h">
          <span className="pub-ico" aria-hidden><CloudArrowUp size={17} weight="duotone" /></span>
          <h2 id={titleId}>Publish to GitHub</h2>
          <span className="pub-spacer" />
          <button type="button" className="pub-x" aria-label="Close" data-testid="publish-close" disabled={busy} onClick={onClose}><X size={15} weight="bold" aria-hidden /></button>
        </div>
        <div className="pub-b">
          {/* from → to */}
          <div className="pub-route" aria-label="From this machine to GitHub">
            <span className="pub-end"><HardDrives size={14} aria-hidden /><span className="pub-mono">{local?.full_name?.split("/")[1] ?? "this project"}</span>
              <small>{local?.branches?.length ?? 1} branch{(local?.branches?.length ?? 1) === 1 ? "" : "es"}{local?.open_change_sets ? ` · ${local.open_change_sets} open change${local.open_change_sets === 1 ? "" : "s"}` : ""}</small></span>
            <ArrowRight size={14} className="pub-arrow" aria-hidden />
            <span className="pub-end" data-gh><GithubLogo size={14} weight="fill" aria-hidden /><span className="pub-mono">{owner || "owner"}/{repo || "repo"}</span>
              <small>{mode === "new" ? (priv ? "new · private" : "new · public") : "existing · empty"}</small></span>
          </div>

          {!done ? (
            <>
              <div className="pub-seg" role="radiogroup" aria-label="New or existing repo">
                <button type="button" role="radio" aria-checked={mode === "new"} data-testid="publish-mode-new" disabled={busy} onClick={() => { setMode("new"); setRefusal(null); }}>New repo</button>
                <button type="button" role="radio" aria-checked={mode === "existing"} data-testid="publish-mode-existing" disabled={busy} onClick={() => { setMode("existing"); setRefusal(null); }}>Existing empty repo</button>
              </div>
              <div className="pub-row">
                <label className="pub-field pub-owner"><span>Owner</span>
                  {accounts.length ? (
                    <select className="pub-input" data-testid="publish-owner" value={owner} disabled={busy} onChange={(e) => { setOwner(e.target.value); setRefusal(null); }}>
                      {[...new Set([...(owner && !accounts.includes(owner) ? [owner] : []), ...accounts])].map((a) => <option key={a} value={a}>{a}</option>)}
                    </select>
                  ) : <input className="pub-input" data-testid="publish-owner" value={owner} disabled={busy} placeholder="your-org" onChange={(e) => setOwner(e.target.value.trim())} />}
                </label>
                <span className="pub-slash" aria-hidden>/</span>
                <label className="pub-field pub-name"><span>Repo name</span>
                  <input className="pub-input pub-mono" data-testid="publish-repo" value={repo} disabled={busy} spellCheck={false} maxLength={100}
                         onChange={(e) => { setRepo(e.target.value.replace(/\s+/g, "-")); setRefusal(null); }} /></label>
              </div>
              {mode === "new" ? (
                <label className="pub-check"><input type="checkbox" data-testid="publish-private" checked={priv} disabled={busy} onChange={(e) => setPriv(e.target.checked)} /><LockKey size={12} aria-hidden /> Private</label>
              ) : null}
              <p className="pub-said" data-testid="publish-check" data-ok={checking ? "checking" : check ? String(check.ok) : "unknown"} role="status">
                {checking ? <span className="pub-faint">Asking GitHub…</span> : check ? <>{check.ok ? <Check size={12} weight="bold" aria-hidden /> : <Hand size={12} weight="fill" aria-hidden />}<span>{check.words}</span></> : null}
                {check && !check.ok && check.reason === "exists" && mode === "new" ? <button type="button" className="pub-link" data-testid="publish-use-existing" onClick={() => setMode("existing")}>Publish into it ›</button> : null}
                {check && !check.ok && check.reason === "failed" ? <button type="button" className="pub-link" data-testid="publish-ask-again" onClick={() => setAskAgain((n) => n + 1)}>Ask again</button> : null}
              </p>
            </>
          ) : null}

          {/* the diagram */}
          <ol className="pub-steps" data-testid="publish-steps" aria-label="Publish steps">
            {PUBLISH_STEPS.map((s, i) => (
              <li key={s.key} data-s={steps[s.key]} data-testid={`publish-step-${s.key}`}>
                <span className="pub-dot" aria-hidden>{steps[s.key] === "done" ? <Check size={11} weight="bold" /> : steps[s.key] === "doing" ? <CircleNotch size={11} className="pub-spin" /> : steps[s.key] === "failed" ? "!" : i + 1}</span>
                <span className="pub-step-l">{s.label}</span>
              </li>
            ))}
          </ol>

          {refusal ? (
            <p className="pub-refused" role="alert" data-testid="publish-refusal">{refusal.message}
              {fix ? <> <a className="pub-link" data-testid="publish-fix" href={fix.href} {...(fix.external ? { target: "_blank", rel: "noreferrer" } : {})}>{fix.label} ›</a></> : null}
              <span className="pub-faint"> Pressing Publish again picks up where it stopped.</span></p>
          ) : null}
          {error ? <p className="pub-refused" role="alert" data-testid="publish-error">{error}</p> : null}
          {done ? (
            <p className="pub-done" data-testid="publish-done" role="status"><Check size={13} weight="bold" aria-hidden /> {result!.words} The project now builds on GitHub; this machine keeps a mirror.
              {Object.keys(result!.pull_requests ?? {}).length ? <> {Object.keys(result!.pull_requests ?? {}).length} open change{Object.keys(result!.pull_requests ?? {}).length === 1 ? "" : "s"} became pull requests.</> : null}</p>
          ) : (
            <p className="pub-faint pub-decision" data-testid="publish-decision"><Hand size={11} aria-hidden /> Your decision: Publish {mode === "new" ? "makes the repo and " : ""}pushes every branch and tag. Nothing is forced.</p>
          )}
        </div>
        <div className="pub-f">
          <span className="pub-spacer" />
          {done ? <button type="button" className="pub-primary" data-testid="publish-finish" onClick={onClose}>Done</button> : (
            <button type="button" className="pub-primary" data-testid="publish-go" disabled={busy || !!blocked} title={blocked || "Publish to GitHub"} onClick={() => void press()}>
              {busy ? <CircleNotch size={15} className="pub-spin" aria-hidden /> : <CloudArrowUp size={15} aria-hidden />}{busy ? "Publishing…" : refusal ? "Try again" : "Publish"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}

/** THE QUIET CHIP on a local project's header: "Local · Publish to GitHub". It reads the
 *  project's local repository once; a `?publish=owner/repo` link opens the sheet straight away. */
export function LocalPublishChip({ projectId, onPublished }: { projectId: string; onPublished?: () => void }) {
  const [local, setLocal] = useState<LocalRepo | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const published = useRef(false);
  useEffect(() => {
    const ctl = new AbortController();
    localFirstApi.localRepo(projectId, ctl.signal).then(setLocal).catch(() => undefined);
    try {
      const asked = new URL(window.location.href).searchParams.get("publish");
      if (asked !== null) setOpen(asked);
    } catch { /* non-browser */ }
    return () => ctl.abort();
  }, [projectId]);
  if (!local?.local && open === null) return null;
  const close = () => {
    setOpen(null);
    if (published.current) { published.current = false; onPublished?.(); }
    try { const u = new URL(window.location.href); if (u.searchParams.has("publish")) { u.searchParams.delete("publish"); window.history.replaceState(window.history.state, "", u.toString()); } } catch { /* non-browser */ }
  };
  return (
    <>
      {local?.local ? (
        <button type="button" className="px-hchip pub-chip" data-testid="px-head-publish" title="The code is kept on this machine. Publish it to GitHub any time: history, open changes and all."
                onClick={() => setOpen("")}>
          <span className="pub-chip-dot" aria-hidden />Local<span className="pub-chip-sep">·</span><CloudArrowUp size={11} aria-hidden />Publish to GitHub
        </button>
      ) : null}
      {/* The page refreshes when the sheet closes, never under it: the chip that holds it goes once
          the project builds on GitHub. */}
      {open !== null && local ? <PublishSheet projectId={projectId} local={local} prefill={open} onClose={close}
                                              onDone={() => { published.current = true; }} /> : null}
    </>
  );
}

/** Settings › Code: a local project's code, and its Publish to GitHub. */
export function LocalCodePanel({ projectId, onPublished }: { projectId: string; onPublished?: () => void }) {
  const [local, setLocal] = useState<LocalRepo | null>(null);
  const [open, setOpen] = useState(false);
  const published = useRef(false);
  useEffect(() => {
    const ctl = new AbortController();
    localFirstApi.localRepo(projectId, ctl.signal).then(setLocal).catch(() => undefined);
    return () => ctl.abort();
  }, [projectId]);
  if (!local || (!local.local && !open)) return null;
  return (
    <div className="pub-panel" data-testid="px-set-local">
      <span className="pub-panel-ico" aria-hidden><HardDrives size={16} weight="duotone" /></span>
      <span className="pub-panel-t"><b>On this machine</b>
        <small>{local.default_branch ?? "main"} · {local.branches?.length ?? 1} branch{(local.branches?.length ?? 1) === 1 ? "" : "es"}{local.open_change_sets ? ` · ${local.open_change_sets} open change${local.open_change_sets === 1 ? "" : "s"}` : ""} · works offline</small></span>
      <button type="button" className="px-btn" data-size="s" data-kind="primary" data-testid="px-set-publish" onClick={() => setOpen(true)}>
        <CloudArrowUp size={13} aria-hidden /> Publish to GitHub</button>
      {open ? <PublishSheet projectId={projectId} local={local} onClose={() => {
                              setOpen(false);
                              if (published.current) { published.current = false; onPublished?.(); localFirstApi.localRepo(projectId).then(setLocal).catch(() => undefined); }
                            }}
                            onDone={() => { published.current = true; }} /> : null}
    </div>
  );
}

