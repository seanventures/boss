"use client";

/**
 * THE WORKFLOW MARKETPLACE: where a project gets more workflows. The Library stays one list
 * (Standard and Yours); this is only where packs come from (engine workflow_marketplace).
 *
 *     "we dont have to ship by default we should have Install button once installed it will be
 *      showing in their custom / local workflows and not in Standard ones"
 *
 * Pack cards: publisher, version, what is in it, and Install / Installed ✓ / Update. Install is
 * confirmed here on the page, with what it adds, needs and brings (never window.confirm). A pack
 * this project published says so and can be unpublished. Amber marks the one thing that needs a
 * person: an update waiting for a workflow you changed, or something a pack needs not set up.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, Storefront } from "@phosphor-icons/react";

import { useCan } from "../../components/identity/identity-provider";
import { notifyChanged } from "../changed";
import { fetchMarketplace, installPack, unpublishPack } from "../../lib/workflow-api";
import { couldNot, reasonOf } from "../../lib/action-result";
import type { MarketPack } from "../../lib/workflow-editor-types";

type Filter = "all" | "boss" | "org";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" }, { key: "boss", label: "BOSS" }, { key: "org", label: "Your org" },
];

const count = (n: number) => `${n} workflow${n === 1 ? "" : "s"}`;

/** "BOSS · v1.0 · 3 workflows" — who, which version, how many. */
export function packMeta(p: MarketPack): string {
  return `${p.source === "boss" ? "BOSS" : "Your org"} · v${p.version} · ${count(p.workflows.length)}`;
}

export function Marketplace({ projectId, onBack, onOpen }: {
  projectId: string;
  onBack: () => void;
  /** Open an installed workflow in the editor. */
  onOpen?: (key: string) => void;
}) {
  const [packs, setPacks] = useState<MarketPack[] | null>(null);
  const [failed, setFailed] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState<{ pack: string; text: string; tone?: "error" } | null>(null);
  const installGate = useCan("workflow:install");
  const publishGate = useCan("workflow:publish");

  const load = useCallback(async () => {
    try {
      setPacks(await fetchMarketplace(projectId));
      setFailed("");
    } catch (e) {
      setFailed(couldNot("load the marketplace", reasonOf(e)));
    }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(
    () => (packs ?? []).filter((p) => filter === "all" || p.source === filter), [packs, filter]);
  const confirming = (packs ?? []).find((p) => p.id === confirm) ?? null;

  const install = async (p: MarketPack) => {
    setBusy(p.id);
    try {
      await installPack(projectId, p.id);
      setConfirm(null);
      setSaid({ pack: p.id, text: "Installed. It is in Yours." });
      notifyChanged("marketplace");
      await load();
    } catch (e) {
      setSaid({ pack: p.id, text: String((e as Error)?.message ?? e), tone: "error" });
    } finally {
      setBusy(null);
    }
  };

  const unpublish = async (p: MarketPack) => {
    setBusy(p.id);
    try {
      await unpublishPack(projectId, p.id);
      notifyChanged("marketplace");
      await load();
    } catch (e) {
      setSaid({ pack: p.id, text: String((e as Error)?.message ?? e), tone: "error" });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="wf-market" data-testid="wf-market" aria-label="Marketplace">
      <div className="wf-market-bar">
        <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-market-back" onClick={onBack}>
          <ArrowLeft size={14} aria-hidden /> Library
        </button>
        <h3 className="wf-market-title"><Storefront size={16} aria-hidden /> Marketplace</h3>
        <span className="wf-market-note">Install puts a pack in Yours.</span>
        <span className="wf-lib-spacer" />
        <div className="wf-seg" role="tablist" aria-label="Publisher">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" role="tab" aria-selected={filter === f.key}
                    data-active={filter === f.key} data-testid={`wf-market-filter-${f.key}`}
                    onClick={() => setFilter(f.key)}>{f.label}</button>
          ))}
        </div>
      </div>

      {failed ? (
        <p className="wf-lib-error" role="alert" data-testid="wf-market-error">
          <span data-user-text>{failed}</span>
          <button type="button" className="wf-link" onClick={() => void load()}>Retry</button>
        </p>
      ) : null}

      {confirming ? (
        <InstallConfirm pack={confirming} busy={busy === confirming.id}
                        error={said?.pack === confirming.id && said.tone === "error" ? said.text : null}
                        blocked={installGate.allowed ? null : installGate.reason}
                        onInstall={() => void install(confirming)} onCancel={() => setConfirm(null)} />
      ) : null}

      {packs === null && !failed ? (
        <p className="wf-lib-muted" data-testid="wf-market-loading">Loading…</p>
      ) : shown.length ? (
        <div className="wf-market-grid" data-testid="wf-market-grid">
          {shown.map((p) => (
            <PackCard key={p.id} pack={p} busy={busy === p.id}
                      said={said?.pack === p.id && p.id !== confirm ? said : null}
                      installBlocked={installGate.allowed ? null : installGate.reason}
                      publishBlocked={publishGate.allowed ? null : publishGate.reason}
                      onInstall={() => { setSaid(null); setConfirm(p.id); }}
                      onUnpublish={() => void unpublish(p)} onOpen={onOpen} />
          ))}
        </div>
      ) : packs ? (
        <p className="wf-lib-muted" data-testid="wf-market-empty">
          {filter === "org" ? "Nothing published in your org yet. Publish one of yours from its ⋯ menu." : "No packs"}
        </p>
      ) : null}
    </section>
  );
}

function PackCard({ pack: p, busy, said, installBlocked, publishBlocked, onInstall, onUnpublish, onOpen }: {
  pack: MarketPack; busy: boolean; said: { text: string; tone?: "error" } | null;
  installBlocked: string | null; publishBlocked: string | null;
  onInstall: () => void; onUnpublish: () => void; onOpen?: (key: string) => void;
}) {
  const [asking, setAsking] = useState(false);
  const one = p.workflows.length === 1 ? p.workflows[0] : null;
  return (
    <article className="wf-pack" data-testid={`wf-pack-${p.id}`} data-installed={p.installed || undefined}>
      <h4 className="wf-pack-name" data-user-text>{p.name}</h4>
      <div className="wf-pack-meta" data-testid={`wf-pack-meta-${p.id}`}
           title={p.source === "org" ? `Published by ${p.publisher}` : undefined}>{packMeta(p)}</div>
      <ul className="wf-pack-list" data-user-text>
        {one ? (
          <li title={one.description || undefined}>
            {one.name}{one.steps.length ? `: ${one.steps.join(" → ")}` : ""}
          </li>
        ) : p.workflows.map((w) => <li key={w.key} title={w.description || undefined}>{w.name}</li>)}
      </ul>
      <div className="wf-pack-act">
        {p.published_here ? (
          <>
            <span className="wf-pack-ok" data-testid={`wf-pack-published-${p.id}`}>Yours · published</span>
            {asking ? (
              <span className="wf-pack-ask" role="alertdialog" aria-label="Unpublish?">
                Unpublish?
                <button type="button" className="wf-btn wf-btn-danger wf-btn-sm" autoFocus
                        data-testid={`wf-pack-unpublish-go-${p.id}`} disabled={busy}
                        title="Installed copies stay" onClick={() => { setAsking(false); onUnpublish(); }}>
                  Unpublish</button>
                <button type="button" className="wf-link" onClick={() => setAsking(false)}>Cancel</button>
              </span>
            ) : (
              <button type="button" className="wf-link" data-testid={`wf-pack-unpublish-${p.id}`}
                      disabled={busy || Boolean(publishBlocked)} title={publishBlocked ?? "Installed copies stay"}
                      onClick={() => setAsking(true)}>{busy ? "Unpublishing…" : "Unpublish"}</button>
            )}
          </>
        ) : p.installed ? (
          <>
            <span className="wf-pack-ok" data-testid={`wf-pack-installed-${p.id}`}>
              Installed <Check size={13} weight="bold" aria-hidden />
            </span>
            {p.update_available ? (
              <span className="wf-tag" data-kind="modified" data-testid={`wf-pack-update-${p.id}`}
                    title="You changed a workflow of this pack. Review the update on it in Yours.">
                Update v{p.update_available}
              </span>
            ) : p.waiting.length ? (
              <span className="wf-tag" data-kind="modified" data-testid={`wf-pack-waiting-${p.id}`}
                    title={p.waiting[0]}>Waiting</span>
            ) : null}
            {onOpen && one ? (
              <button type="button" className="wf-link" onClick={() => onOpen(one.key)}>Open</button>
            ) : null}
          </>
        ) : (
          <button type="button" className="wf-btn wf-btn-primary wf-btn-sm" data-testid={`wf-pack-install-${p.id}`}
                  disabled={busy || Boolean(installBlocked)} title={installBlocked ?? undefined}
                  onClick={onInstall}>Install</button>
        )}
      </div>
      {said ? (
        <p className="wf-pack-said" role={said.tone === "error" ? "alert" : "status"} data-tone={said.tone}
           data-testid={`wf-pack-said-${p.id}`}>{said.text}</p>
      ) : null}
    </article>
  );
}

/** Install, confirmed on the page: what it adds to Yours, what it needs here, which agents. */
function InstallConfirm({ pack: p, busy, blocked, error, onInstall, onCancel }: {
  pack: MarketPack; busy: boolean; blocked: string | null; error: string | null;
  onInstall: () => void; onCancel: () => void;
}) {
  // Pressed on a card further down (a phone's one column): bring the confirm into view.
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => { ref.current?.scrollIntoView?.({ block: "nearest" }); }, [p.id]);
  return (
    <div className="wf-market-confirm" role="dialog" aria-label={`Install ${p.name}`} ref={ref}
         data-testid="wf-market-confirm"
         onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}>
      <strong>Install {p.name} v{p.version}?</strong>
      <dl className="wf-market-kv">
        <dt>Adds to Yours</dt>
        <dd data-user-text data-testid="wf-market-adds">{p.workflows.map((w) => w.name).join(" · ")}</dd>
        {p.needs.length ? (
          <>
            <dt>Needs</dt>
            <dd data-testid="wf-market-needs">
              {p.needs.map((n, i) => (
                <span key={n.key}>
                  {i ? " · " : ""}{n.label}{" "}
                  {n.ok && !n.note ? <Check size={12} weight="bold" aria-label="set up" />
                    : n.ok ? <em className="wf-market-dim">({n.note})</em>
                    : <span className="wf-tag" data-kind="modified" title="Its workflows wait until it is set up">{n.note ?? "not set up"}</span>}
                </span>
              ))}
              {p.needs.some((n) => !n.ok) ? <span className="wf-market-dim"> — runs wait until it is</span> : null}
            </dd>
          </>
        ) : null}
        {p.agents.length ? (
          <>
            <dt>Agents</dt>
            <dd data-testid="wf-market-agents">
              {p.agents.map((a) => a.name).join(", ")}
              {p.agents.some((a) => !a.present) ? <span className="wf-market-dim"> (added if missing)</span> : null}
            </dd>
          </>
        ) : null}
      </dl>
      {error ? <p className="wf-publish-error" role="alert" data-testid="wf-market-error-said">{error}</p> : null}
      <div className="wf-market-actions">
        <button type="button" className="wf-btn wf-btn-primary" autoFocus data-testid="wf-market-install"
                disabled={busy || Boolean(blocked)} title={blocked ?? undefined} onClick={onInstall}>
          {busy ? "Installing…" : "Install"}
        </button>
        <button type="button" className="wf-btn" data-testid="wf-market-cancel" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
