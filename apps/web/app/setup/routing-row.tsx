"use client";

/**
 * Setup › Model routing (docs/product/MODEL_ROUTER.md) — where "Who does what" lived.
 *
 * Smart | Fixed. Smart picks a tier per step from the task (Fast / Balanced / Frontier), holds
 * the floors, moves cheap work off a subscription near its limit and escalates on evidence.
 * Fixed is the old page: each job pinned as before (Pin a job), the profile as its preset.
 * Every write is one PUT of a partial policy; the server answers with the whole view.
 */
import { GatedButton } from "../../components/gated";
import React, { forwardRef, useEffect, useMemo, useState } from "react";
import { CaretRight, Plus, X } from "@phosphor-icons/react";

import { shortLabel } from "../../lib/model-routes";
import { SetupFailure, setupWrite } from "../../lib/setup-client";
import { useGate } from "../../components/gated";
import type {
  HeadroomRule, Routing, RouteTarget, SetupView, Tier,
} from "../../lib/setup-types";
import { JobLine } from "./jobs-row";
import { toast, useBoard } from "./parts";
import { Tip } from "./pill";
import { SetupRow } from "./setup-row";

const TIERS: Tier[] = ["fast", "balanced", "frontier"];
const TIER_LABEL: Record<Tier, string> = { fast: "Fast", balanced: "Balanced", frontier: "Frontier" };
const STATE_TONE: Record<string, string> = {
  plenty: "green", tight: "amber", near_limit: "amber", exhausted: "red",
};
const MODE_TIP = {
  smart: "Picks a tier per step from the task, then a source with headroom.",
  fixed: "Each job runs where you pinned it, as before.",
};
const enc = (t: { engine: string; model: string | null }) => `${t.engine}::${t.model ?? ""}`;
const dec = (v: string) => {
  const [engine, model] = v.split("::");
  return { engine, model: model || null };
};

function useSave() {
  const { projectId, view, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  // Routing is PUT /v1/setup/routing (setup:manage): a viewer without it sees every control
  // marked unavailable with the reason, and a press does nothing.
  const gate = useGate("setup:manage");
  const save = async (policy: Record<string, unknown>, label: string) => {
    // Controls are never disabled while saving — a disabled button drops keyboard focus to
    // <body>. A second press during a save is ignored here instead.
    if (busy || !gate.allowed) return false;
    setBusy(true);
    try {
      const next = await setupWrite<SetupView>(projectId, "routing", {
        policy, version: view.routing?.version ?? null }, { label });
      apply(next);
      toast(`Saved · ${label}`);
      return true;
    } catch (error) {
      const refusal = (error as SetupFailure).refusal as { headline?: string; message?: string }
        | undefined;
      toast(refusal?.message ?? refusal?.headline ?? "Not saved", "error");
      return false;
    } finally { setBusy(false); }
  };
  return { save, busy, gate };
}

/** Focus a control by test id after the next render — for when the pressed one unmounted
 *  (a removed chip, a closed popover), so keyboard focus never falls to <body>. */
function useRefocus(): (testid: string) => void {
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    if (!target) return;
    const el = document.querySelector<HTMLElement>(`[data-testid="${target}"]`);
    el?.focus();
    setTarget(null);
  });
  return setTarget;
}

function TierDot({ tier }: { tier: Tier }) {
  return <span className={`mr-dot mr-${tier}`} aria-hidden="true" />;
}

/** Every source · model a tier can name, from what is connected. */
function useOptions(routing: Routing): RouteTarget[] {
  return useMemo(() => {
    const out: RouteTarget[] = [];
    for (const s of routing.sources) {
      for (const m of s.models.length ? s.models : [null]) {
        out.push({ engine: s.engine, model: m, label: m ? `${s.label} · ${m}` : s.label });
      }
    }
    return out;
  }, [routing.sources]);
}

function TierCard({ routing, tier }: { routing: Routing; tier: (Routing["tiers"])[number] }) {
  const options = useOptions(routing);
  const { save, busy, gate } = useSave();
  const [primary, fallback] = [tier.targets[0], tier.targets[1]];
  const withCurrent = (t?: RouteTarget) =>
    t && !options.some((o) => enc(o) === enc(t)) ? [t, ...options] : options;
  const write = (targets: (RouteTarget | undefined)[]) => {
    const seen = new Set<string>();
    const unique = targets.filter((t): t is RouteTarget => {
      if (!t || seen.has(enc(t))) return false;
      seen.add(enc(t));
      return true;
    });
    return save({ tiers: { [tier.tier]: unique.map((t) => ({ engine: t.engine, model: t.model })) } },
                `${tier.label} tier`);
  };
  return (
    <div className="mr-tier" data-testid={`mr-tier-${tier.tier}`}>
      <Tip tip={tier.criteria} className="mr-tier-name"><TierDot tier={tier.tier} />{tier.label}</Tip>
      <select {...gate.props} aria-label={`${tier.label} model`} value={primary ? enc(primary) : ""} aria-busy={busy || undefined}
              data-testid={`mr-tier-select-${tier.tier}`}
              onChange={(e) => {
                const next = dec(e.target.value) as RouteTarget;
                // Choosing the fallback as the first swaps them, rather than losing one.
                void write([next, fallback && enc(fallback) === enc(next) ? primary : fallback]);
              }}>
        {!primary ? <option value="">Choose…</option> : null}
        {withCurrent(primary).map((o) => <option key={enc(o)} value={enc(o)} title={o.label}>{shortLabel(o.label)}</option>)}
      </select>
      <label className="mr-fallback" title="Used when the first is out of headroom or failing.">
        <span className="su-faint">then</span>
        <select {...gate.props} aria-label={`${tier.label} fallback`} value={fallback ? enc(fallback) : ""}
                aria-busy={busy || undefined} data-testid={`mr-tier-fallback-${tier.tier}`}
                onChange={(e) => void write([primary,
                  e.target.value ? dec(e.target.value) as RouteTarget : undefined])}>
          <option value="">None</option>
          {withCurrent(fallback).filter((o) => !primary || enc(o) !== enc(primary))
            .map((o) => <option key={enc(o)} value={enc(o)} title={o.label}>{shortLabel(o.label)}</option>)}
        </select>
      </label>
    </div>
  );
}

function Floors({ routing, smart }: { routing: Routing; smart: boolean }) {
  const { save, busy, gate } = useSave();
  const [adding, setAdding] = useState(false);
  const [step, setStep] = useState(routing.floor_steps[0]?.step ?? "implement");
  const [tier, setTier] = useState<Tier>("frontier");
  const refocus = useRefocus();
  const floors = routing.floors.map(({ match, tier: t, label }) => ({ match, tier: t, ...(label ? { label } : {}) }));
  return (
    <div className="mr-section" data-testid="mr-floors">
      <div className="mr-line">
        <span className="mr-sub" title="Minimum tiers. Profiles are presets.">Floors</span>
        <select {...gate.props} aria-label="Floor preset" value={routing.floor_preset} aria-busy={busy || undefined}
                data-testid="mr-preset" title="The pipeline profile, as floors."
                onChange={(e) => void save({ floor_preset: e.target.value }, "Floors")}>
          {routing.presets.map((p) => <option key={p.name} value={p.name}>{p.label}</option>)}
          {!routing.presets.some((p) => p.name === routing.floor_preset)
            ? <option value={routing.floor_preset}>Custom</option> : null}
        </select>
      </div>
      {smart ? (
        <div className="mr-line mr-chips">
          {routing.floors.map((f, i) => (
            <span key={`${f.describe}-${i}`} className={`mr-chip mr-chip-${f.tier}`}
                  data-testid="mr-floor">
              <strong>{f.describe}</strong>
              <span>≥ {f.tier_label}</span>
              <GatedButton permission="setup:manage" type="button" className="mr-x" aria-label={`Remove ${f.describe} floor`}
                      aria-busy={busy || undefined}
                      data-testid={`mr-floor-x-${i}`}
                      onClick={async () => {
                        await save({ floor_preset: "custom",
                                     floors: floors.filter((_, j) => j !== i) }, "Floors");
                        refocus(i < routing.floors.length - 1 ? `mr-floor-x-${i}` : "mr-floor-add");
                      }}>
                <X size={11} aria-hidden="true" />
              </GatedButton>
            </span>
          ))}
          {adding ? (
            <span className="su-popover mr-add" role="group" aria-label="Add a floor"
                  onKeyDown={(e) => {
                    if (e.key === "Escape") { e.stopPropagation(); setAdding(false); refocus("mr-floor-add"); }
                  }}>
              <select aria-label="Step" value={step} onChange={(e) => setStep(e.target.value)}
                      data-testid="mr-floor-step" autoFocus>
                {routing.floor_steps.map((s) => <option key={s.step} value={s.step}>{s.label}</option>)}
              </select>
              <select aria-label="Minimum tier" value={tier}
                      onChange={(e) => setTier(e.target.value as Tier)} data-testid="mr-floor-tier">
                {TIERS.map((t) => <option key={t} value={t}>≥ {TIER_LABEL[t]}</option>)}
              </select>
              <GatedButton permission="setup:manage" type="button" className="su-btn su-btn-sm su-btn-primary" aria-busy={busy || undefined}
                      data-testid="mr-floor-save"
                      onClick={async () => {
                        if (await save({ floor_preset: "custom",
                          floors: [...floors, { match: { step }, tier }] }, "Floors")) {
                          setAdding(false);
                          refocus("mr-floor-add");
                        }
                      }}>Add</GatedButton>
              <GatedButton permission="setup:manage" type="button" className="su-link"
                      onClick={() => { setAdding(false); refocus("mr-floor-add"); }}>Cancel</GatedButton>
            </span>
          ) : (
            <GatedButton permission="setup:manage" type="button" className="mr-chip mr-chip-add" data-testid="mr-floor-add"
                    onClick={() => setAdding(true)}><Plus size={11} aria-hidden="true" />Floor</GatedButton>
          )}
        </div>
      ) : null}
    </div>
  );
}

function Meter({ h }: { h: Routing["headroom"][number] }) {
  const left = h.left == null ? null : Math.round(h.left * 100);
  const resets = h.resets_at ? new Date(h.resets_at) : null;
  const when = resets && !Number.isNaN(resets.getTime())
    ? resets.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : null;
  const tip = `${h.source_label}: ${h.describe}${when ? ` · resets ${when}` : ""}`
    + (h.basis === "estimate" ? " (estimated from calls)" : h.basis === "unknown" ? " (not reported yet)" : "");
  return (
    <div className="mr-line" data-testid={`mr-headroom-${h.engine}`}>
      <span className="mr-source">{h.source_label}</span>
      <span className="mr-meter" role="meter" aria-label={`${h.source_label} headroom`}
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={left ?? undefined}
            title={tip} data-tip={tip}>
        <i style={{ width: `${left ?? 100}%` }} data-state={h.state} />
      </span>
      <span className={`su-pill su-tone-${STATE_TONE[h.state] ?? "grey"}`} title={tip} data-tip={tip}
            data-testid={`mr-headroom-state-${h.engine}`}>
        <span className={`su-dot su-tone-${STATE_TONE[h.state] ?? "grey"}`} aria-hidden="true" />
        {h.label}{left != null && h.state !== "exhausted" ? ` · ${left}% left` : ""}
      </span>
      {when ? <span className="su-faint">resets {when}</span> : null}
    </div>
  );
}

function Headroom({ routing, metersOnly = false }: { routing: Routing; metersOnly?: boolean }) {
  const { save, busy, gate } = useSave();
  const refocus = useRefocus();
  const rule: HeadroomRule | undefined = routing.headroom_rules[0];
  const subs = routing.headroom;
  const targets = routing.sources.filter((s) => s.kind !== "subscription");
  if (!subs.length) return null;
  if (metersOnly) {
    // Fixed mode moves nothing, but how much of each subscription is left is still worth
    // seeing — it is the same account the operator's own work spends.
    return (
      <div className="mr-section" data-testid="mr-headroom">
        <span className="mr-sub" title="How much of each subscription's window is left.">Headroom</span>
        {subs.map((h) => <Meter key={h.engine} h={h} />)}
      </div>
    );
  }
  const write = (next: HeadroomRule | null) => save({ headroom: next ? [next] : [] }, "Headroom");
  const draft: HeadroomRule = rule ?? {
    source: subs[0].engine, when: "near_limit", tiers: ["fast", "balanced"],
    to: (targets.find((t) => t.engine === "openrouter") ?? targets[0])?.engine ?? "openrouter" };
  return (
    <div className="mr-section" data-testid="mr-headroom">
      <span className="mr-sub" title="How much of each subscription's weekly window is left.">Headroom</span>
      {subs.map((h) => <Meter key={h.engine} h={h} />)}
      {rule ? (
        <div className="mr-line mr-rule" data-testid="mr-rule">
          <span>When</span>
          <select {...gate.props} aria-label="When" value={rule.when} aria-busy={busy || undefined} data-testid="mr-rule-when"
                  onChange={(e) => void write({ ...rule, when: e.target.value })}>
            {routing.headroom_states.filter((s) => s.state !== "exhausted")
              .map((s) => <option key={s.state} value={s.state}>{s.label.toLowerCase()}</option>)}
          </select>
          <span>move</span>
          {(["fast", "balanced"] as Tier[]).map((t) => {
            const on = rule.tiers.includes(t);
            return (
              <GatedButton permission="setup:manage" key={t} type="button" className={`mr-chip mr-toggle mr-chip-${t}`}
                      aria-pressed={on} aria-busy={busy || undefined} data-testid={`mr-rule-tier-${t}`}
                      onClick={() => {
                        const tiers = on ? rule.tiers.filter((x) => x !== t) : [...rule.tiers, t];
                        void (tiers.length ? write({ ...rule, tiers }) : write(null));
                      }}>{TIER_LABEL[t]}</GatedButton>
            );
          })}
          <span>to</span>
          <select aria-label="Move to" value={rule.to} aria-busy={busy || undefined} data-testid="mr-rule-to"
                  onChange={(e) => void write({ ...rule, to: e.target.value })}>
            {targets.map((s) => <option key={s.engine} value={s.engine}>{s.label}</option>)}
            {!targets.some((s) => s.engine === rule.to)
              ? <option value={rule.to}>{rule.to}</option> : null}
          </select>
          <Tip tip="Frontier work and floors keep the subscription until it is exhausted."
               className="mr-chip mr-chip-frontier mr-keep">Frontier stays</Tip>
          <GatedButton permission="setup:manage" type="button" className="mr-x" aria-label="Remove headroom rule" aria-busy={busy || undefined}
                  data-testid="mr-rule-x"
                  onClick={async () => { await write(null); refocus("mr-rule-add"); }}>
            <X size={11} aria-hidden="true" /></GatedButton>
        </div>
      ) : targets.length ? (
        <GatedButton permission="setup:manage" type="button" className="mr-chip mr-chip-add" data-testid="mr-rule-add"
                onClick={async () => { await write(draft); refocus("mr-rule-when"); }}>
          <Plus size={11} aria-hidden="true" />Move cheap work near limit
        </GatedButton>
      ) : (
        <Tip tip="Connect OpenRouter or a local model to move cheap work off a subscription."
             className="su-faint">No other source</Tip>
      )}
    </div>
  );
}

function Classifier({ routing }: { routing: Routing }) {
  const { save, busy, gate } = useSave();
  const c = routing.classifier;
  const [criteria, setCriteria] = useState(c.criteria);
  useEffect(() => setCriteria(c.criteria), [c.criteria]);
  const jev = c.jev === "held" ? { label: "Jev · paused", tip: "Live Jev calls are held (BOSS-451). Rules answer instead." }
    : c.jev === "ready" ? { label: "Jev · ready", tip: "TypeSafe Jev classifies first." }
    : { label: "No Jev key", tip: "Add a TypeSafe key under Decisions to use Jev." };
  return (
    <div className="mr-section" data-testid="mr-classifier">
      <div className="mr-line">
        <span className="mr-sub" title="Reads the task and picks a tier.">Classifier</span>
        <select aria-label="Classifier" value={c.backend} aria-busy={busy || undefined} data-testid="mr-classifier-backend"
                onChange={(e) => void save({ classifier: { backend: e.target.value } }, "Classifier")}>
          {c.backends.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
        </select>
        <Tip tip={jev.tip} className="mr-chip" testid="mr-jev">{jev.label}</Tip>
        <label className="su-consent" title="When the rules are unsure, ask a Fast model. A small paid call.">
          <input type="checkbox" checked={c.model_fallback} aria-busy={busy || undefined}
                 data-testid="mr-model-fallback"
                 onChange={(e) => void save({ classifier: { model_fallback: e.target.checked } },
                                            "Classifier")} />
          Ask a model if unsure
        </label>
      </div>
      <details className="mr-criteria" data-testid="mr-criteria">
        <summary>Criteria</summary>
        {TIERS.map((t) => (
          <label key={t} className="su-field">
            <span className="su-field-label"><TierDot tier={t} />{TIER_LABEL[t]}</span>
            <textarea rows={2} value={criteria[t] ?? ""} aria-label={`${TIER_LABEL[t]} criteria`}
                      data-testid={`mr-criteria-${t}`}
                      onChange={(e) => setCriteria({ ...criteria, [t]: e.target.value })} />
          </label>
        ))}
        <GatedButton permission="setup:manage" type="button" className="su-btn su-btn-sm su-btn-primary" aria-busy={busy || undefined}
                data-testid="mr-criteria-save"
                onClick={() => void save({ classifier: { criteria } }, "Criteria")}>Save criteria</GatedButton>
      </details>
    </div>
  );
}

function Week({ routing }: { routing: Routing }) {
  const w = routing.week;
  const total = TIERS.reduce((n, t) => n + (w.tiers[t] ?? 0), 0);
  const mix = TIERS.map((t) => `${TIER_LABEL[t]} ${total ? Math.round(((w.tiers[t] ?? 0) / total) * 100) : 0}%`).join(" · ");
  return (
    <div className="mr-section" data-testid="mr-week">
      <span className="mr-sub" title="Routes in the last 7 days.">This week</span>
      <div className="mr-line">
        <span className="mr-mix" role="img" aria-label={`Tier mix: ${mix}`} title={mix} data-tip={mix}>
          {total ? TIERS.map((t) => (
            <i key={t} className={`mr-${t}`} style={{ width: `${((w.tiers[t] ?? 0) / total) * 100}%` }} />
          )) : <i className="mr-none" />}
        </span>
        <span className="mr-stat"><strong>{w.routes}</strong> <span className="su-faint">routes</span></span>
        <Tip tip="Estimated against list prices if every call had been Frontier." className="mr-stat">
          <strong>${w.saved_usd < 10 ? w.saved_usd.toFixed(2) : w.saved_usd.toFixed(0)}</strong> <span className="su-faint">saved</span>
        </Tip>
        <span className="mr-stat"><strong>{w.escalations}</strong> <span className="su-faint">escalated</span></span>
        <span className="mr-stat"><strong>{w.moved}</strong> <span className="su-faint">moved off</span></span>
      </div>
    </div>
  );
}

const HARD_JOBS = new Set(["build", "review", "chat"]);
const PREFER_TIP = "Smart treats a pin as a preference: floors raise it, limits can move it.";
const ALWAYS_TIP = "Always this model, even near or past its limit. Work may wait.";

function Pins({ open, onToggle, routing }: {
  open: boolean; onToggle: () => void; routing?: Routing | null;
}) {
  const { view } = useBoard();
  const { save, busy, gate } = useSave();
  const pinned = view.jobs.filter((j) => j.choice).length;
  const smart = routing?.mode === "smart";
  const hard = new Set(routing?.hard_pins ?? []);
  return (
    <div className="mr-section" data-testid="mr-pins">
      <button type="button" className="su-link su-more" aria-expanded={open} data-testid="su-more-jobs"
              onClick={onToggle} title={smart ? PREFER_TIP : "Which model does each job."}>
        {smart ? "Prefer for a job" : "Pin a job"}
        <CaretRight size={12} className={open ? "su-rot" : undefined} aria-hidden="true" />
        {pinned ? <span className="su-faint"> · {pinned} {smart ? "preferred" : "pinned"}</span> : null}
      </button>
      {open ? (
        <div className="su-jobs">
          {view.jobs.map((job) => (
            <div key={job.job} className="mr-pin">
              <JobLine job={job} />
              {smart && job.choice && HARD_JOBS.has(job.job) ? (
                <label className="su-consent mr-always" title={ALWAYS_TIP} data-tip={ALWAYS_TIP}>
                  <input type="checkbox" checked={hard.has(job.job)} aria-busy={busy || undefined}
                         data-testid={`mr-always-${job.job}`}
                         onChange={(e) => void save({ hard_pins: e.target.checked
                           ? [...hard, job.job] : [...hard].filter((j) => j !== job.job) },
                           e.target.checked ? `Always ${job.label}` : `Prefer ${job.label}`)} />
                  Always
                  {hard.has(job.job) ? <span className="su-chip su-chip-metered" role="note">Even when limited</span> : null}
                </label>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Summary({ routing }: { routing: Routing }) {
  const tight = routing.headroom.find((h) => h.state !== "plenty");
  return (
    <>
      <span className="mr-chip" data-testid="mr-mode-chip">{routing.mode === "smart" ? "Smart" : "Fixed"}</span>
      {routing.mode === "smart" ? routing.tiers.map((t) => (
        <Tip key={t.tier} tip={`${t.label}: ${t.targets.map((x) => x.label).join(", then ") || "none"}`}
             className="mr-chip"><TierDot tier={t.tier} />{t.targets[0]?.model ?? t.targets[0]?.label ?? "—"}</Tip>
      )) : null}
      {tight ? (
        <span className={`su-pill su-tone-${STATE_TONE[tight.state]}`} title={tight.describe}>
          {tight.source_label} · {tight.label}</span>
      ) : null}
    </>
  );
}

export const RoutingRow = forwardRef<HTMLElement, { mode: string; focused: boolean }>(
  function RoutingRow({ mode, focused }, ref) {
    const { view } = useBoard();
    const routing = view.routing;
    const [open, setOpen] = useState(false);
    // Pin a job is open on Fixed (it IS the configuration there) and on a deep link.
    const [pins, setPins] = useState<boolean | null>(null);
    useEffect(() => { if (focused) { setPins(true); setOpen(true); } }, [focused]);
    const { save, busy, gate } = useSave();
    const collapsed = mode === "collapsed" && !open && !focused;
    const smart = routing?.mode === "smart";
    const none = view.models.length === 0;
    return (
      <SetupRow ref={ref} row="jobs" label="Model routing" mode={collapsed ? "collapsed" : "open"}
                focused={focused} tip="Which model runs each piece of work.">
        {!routing ? (
          // An API without the router: the jobs, as overrides, and nothing else.
          <Pins open={pins ?? focused} onToggle={() => setPins((v) => !(v ?? focused))}
                routing={routing} />
        ) : collapsed ? (
          <div className="su-line" data-testid="mr-collapsed">
            <Summary routing={routing} />
            <button type="button" className="su-link" data-testid="su-jobs-change"
                    onClick={() => setOpen(true)}>Change</button>
          </div>
        ) : (
          <div className="mr-body" data-testid="mr-body" data-mode={routing.mode}>
            <div className="mr-line">
              <div className="mr-seg" role="group" aria-label="Routing">
                {(["smart", "fixed"] as const).map((m) => (
                  <GatedButton permission="setup:manage" key={m} type="button" aria-pressed={routing.mode === m} disabled={none} aria-busy={busy || undefined}
                          data-testid={`mr-mode-${m}`} title={MODE_TIP[m]} data-tip={MODE_TIP[m]}
                          onClick={() => routing.mode !== m && void save({ mode: m },
                            m === "smart" ? "Smart routing" : "Fixed routing")}>
                    {m === "smart" ? "Smart" : "Fixed"}
                  </GatedButton>
                ))}
              </div>
              <span className="su-faint">{none ? "Connect a model first" : smart ? "Picks per step" : "As pinned per job"}</span>
            </div>
            {smart ? (
              <div className="mr-section">
                <span className="mr-sub" title="What each tier runs on, then its fallback.">Tiers</span>
                <div className="mr-tiers">
                  {routing.tiers.map((t) => <TierCard key={t.tier} routing={routing} tier={t} />)}
                </div>
              </div>
            ) : null}
            <Floors routing={routing} smart={smart} />
            <Headroom routing={routing} metersOnly={!smart} />
            {smart ? <Classifier routing={routing} /> : null}
            <Pins routing={routing} open={pins ?? (focused || !smart)}
                  onToggle={() => setPins((v) => !(v ?? (focused || !smart)))} />
            <Week routing={routing} />
          </div>
        )}
      </SetupRow>
    );
  });
