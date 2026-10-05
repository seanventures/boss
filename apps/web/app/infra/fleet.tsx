"use client";

/**
 * Infra › Fleet (spec §4.3-§4.5): every machine as a rack with what is placed on it, the NOT
 * PLACED rack for targets no machine of ours runs, the host and target drawers (URL state
 * `host=` / `target=`), the header's `+ Add ▾` sheets and `Ask BOSS`.
 *
 * One poll feeds it all (`useFleet`, owned by the home, ETag'd; a 304 is no render). Writes post
 * through infra-actions (apiWrite → /v1) and then `notifyChanged("infra")`, which re-polls every
 * Infra and Observability view. A control that cannot work here is not drawn: outward SSH off
 * hides every action that reaches a machine; a read-only viewer sees no write at all.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import type { Polled } from "../../lib/factory-floor";
import type { Fleet, Rack, Resident, StageAction } from "../../lib/infra-view";
import { openDockAbout } from "../../lib/dock-subject";
import { navigate } from "../agents/live-util";
import { showToast } from "../client-forms";
import { EnrollSheet, HostSheet, HwAccountSheet } from "./add-sheets";
import { HostDrawer } from "./host-drawer";
import { afterWrite, generateHostKey, provisionHost, verifyHost } from "./infra-actions";
import type { InfraTabProps } from "./infra-home";
import { ErrorLine, Menu, useNow } from "./infra-util";
import { Empty, LiveRegion, Skeleton, useAnnouncer, ZoneHead } from "./kit";
import { RackView, UnplacedRack } from "./rack";
import { SecretsSheet } from "./secrets-sheet";
import { TargetDrawer } from "./target-drawer";
import { AppTargetSheet } from "./target-sheet";

// ── `+ Add ▾` is opened from the header and from the racks (`+ Place app`, `Add host`) ──────

export type AddItem = "host" | "enroll" | "target" | "secret" | "hwaccount";
const ADD_EVENT = "boss:infra-add";
export function openInfraAdd(item: AddItem, hostId?: string | null): void {
  try { window.dispatchEvent(new CustomEvent(ADD_EVENT, { detail: { item, hostId: hostId ?? null } })); } catch { /* not a browser */ }
}

/** Find narrows racks and tiles by host, address, app, env (the server's names, as typed). */
export function filterRacks(racks: Rack[], q: string): Rack[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return racks;
  const hit = (...xs: (string | null | undefined)[]) => xs.some((x) => (x ?? "").toLowerCase().includes(needle));
  const out: Rack[] = [];
  for (const r of racks) {
    if (hit(r.name, r.address, r.arch)) { out.push(r); continue; }
    const residents = r.residents.filter((x) => hit(x.name, x.env, x.id));
    if (residents.length) out.push({ ...r, residents });
  }
  return out;
}

export function FleetTab({ projectId, actor, url, go, query, fleet }: InfraTabProps & { fleet: Polled<Fleet> }) {
  const now = useNow(30_000);
  const [showRetired, setShowRetired] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const f = fleet.data;
  const pre = f?.preconditions;
  const canWrite = pre?.canWrite ?? false;

  // §7: a polite announcement when an app's or a machine's state word changes between polls
  // (`invoice prod is down`), at most one per 10 s; counts are never announced.
  const [said, say] = useAnnouncer();
  const words = useRef<Map<string, string> | null>(null);
  useEffect(() => {
    if (!f) return;
    const next = new Map<string, string>();
    for (const r of f.racks) {
      next.set(`h:${r.id}`, `${r.name} is ${r.state.word.toLowerCase()}`);
      for (const x of r.residents) if (x.kind === "target") next.set(`t:${x.id}`, `${x.name} ${x.env ?? ""} is ${x.health.word.toLowerCase()}`.replace("  ", " "));
    }
    const prev = words.current;
    words.current = next;
    if (!prev) return;
    for (const [k, line] of next) if (prev.has(k) && prev.get(k) !== line) { say(line); break; }
  }, [f, say]);

  const visible = useMemo(() => {
    const all = (f?.racks ?? []).filter((r) => showRetired || r.state.tone !== "none");
    return filterRacks(all, query);
  }, [f, showRetired, query]);
  const unplaced = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (f?.unplaced ?? []).filter((r) => !needle || `${r.name} ${r.env ?? ""}`.toLowerCase().includes(needle));
  }, [f, query]);

  if (!f) {
    return fleet.loading || !fleet.error ? <Skeleton testId="infra-fleet-loading" /> : <Empty testId="infra-fleet-unreachable">Nothing to show</Empty>;
  }

  const canAct = (a: StageAction): boolean => {
    if (!canWrite || !a) return false;
    if (a === "verify") return !!pre?.outwardSshAllowed;
    if (a === "install_docker") return !!pre?.hostWritesAllowed;
    return true;
  };
  const onStage = async (rack: Rack, a: StageAction) => {
    if (a === "copy_command" || !a) { go({ host: rack.id, target: null }); return; }
    setBusy(rack.id); setError(null);
    const r = a === "verify" ? await verifyHost(rack.id, actor)
      : a === "install_docker" ? await provisionHost(rack.id, actor)
      : await generateHostKey(rack.id, actor, "database");
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    if (a === "verify") showToast("success", "Verified");
    afterWrite(fleet.refresh);
    if (a === "generate_key") go({ host: rack.id, target: null });
  };
  const onResident = (r: Resident) => {
    if (r.kind === "target") go({ target: r.id, host: null });
    else if (r.kind === "preview") go({ view: "previews", preview: r.id, host: null, target: null });
    else navigate({ area: "observability", view: "sources", host: null, target: null });
  };
  const onPlace = canWrite ? (hostId: string) => openInfraAdd("target", hostId) : null;
  const windowEnd = f.generatedAt;
  const shown = (f.racks ?? []).filter((r) => r.state.tone !== "none").length;

  return (
    <div className="inf-fleet" data-testid="infra-fleet">
      {!pre?.outwardSshAllowed ? (
        <div className="inf-notice" data-testid="inf-ssh-off" title={pre?.blocker ?? "BOSS may not reach machines over SSH on this install"}>
          SSH out is off
        </div>
      ) : null}
      {pre && !pre.sandboxDeploysEnabled ? (
        <div className="inf-notice" data-testid="inf-sandbox-off"
             title="Merge-triggered sandbox deploys are off (BOSS_ENABLE_SANDBOX_DEPLOY=false)">Sandbox deploys off</div>
      ) : null}
      <ErrorLine error={error} testId="inf-fleet-error" />
      <LiveRegion text={said} />

      <section className="inf-zone" aria-labelledby="inf-machines">
        <ZoneHead id="inf-machines" label="MACHINES" count={shown}>
          {f.retiredCount > 0 ? (
            <button type="button" className="inf-link" aria-pressed={showRetired} data-testid="inf-show-retired"
                    title={`${f.retiredCount} retired`} onClick={() => setShowRetired((v) => !v)}>
              {showRetired ? "Hide retired" : "Show retired"}
            </button>
          ) : null}
        </ZoneHead>
        {visible.length === 0 && !query ? (
          <Empty testId="infra-fleet-empty">
            <span>No machines yet</span>
            {canWrite && pre?.outwardSshAllowed ? (
              <button type="button" className="ag-btn" data-testid="inf-empty-add-host" onClick={() => openInfraAdd("host")}>Add host</button>
            ) : null}
          </Empty>
        ) : null}
        {visible.length === 0 && query ? <Empty testId="infra-fleet-nomatch">Nothing matches</Empty> : null}
        <div className="inf-racks">
          {visible.map((rack) => (
            <RackView key={rack.id} rack={rack} now={now} windowEnd={windowEnd} canAct={canAct} busy={busy === rack.id}
                      onHost={(id) => go({ host: id, target: null })} onResident={onResident} onPlace={onPlace} onStage={(r, a) => void onStage(r, a)} />
          ))}
          {unplaced.length ? <UnplacedRack residents={unplaced} now={now} windowEnd={windowEnd} onResident={onResident} /> : null}
        </div>
      </section>

      {url.host ? (
        <HostDrawer key={url.host} projectId={projectId} actor={actor} hostId={url.host} canWrite={canWrite}
                    onClose={() => go({ host: null })} onResident={onResident} />
      ) : null}
      {url.target ? (
        <TargetDrawer key={url.target} projectId={projectId} actor={actor} targetId={url.target} canWrite={canWrite}
                      racks={f.racks} onClose={() => go({ target: null })} go={go} />
      ) : null}
    </div>
  );
}

// ── `+ Add ▾` ───────────────────────────────────────────────────────────────────────────────

export function InfraAddMenu({ projectId, actor, go, fleet }: InfraTabProps & { fleet: Polled<Fleet> }) {
  const [open, setOpen] = useState<{ item: AddItem; hostId: string | null } | null>(null);
  const pre = fleet.data?.preconditions;
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ item: AddItem; hostId: string | null }>).detail;
      if (d?.item) setOpen({ item: d.item, hostId: d.hostId ?? null });
    };
    window.addEventListener(ADD_EVENT, on);
    return () => window.removeEventListener(ADD_EVENT, on);
  }, []);
  if (!fleet.data) return null;
  if (!pre?.canWrite) {
    return <span className="inf-chip" data-testid="infra-read-only" title="You lack providerops_manage">Read only</span>;
  }
  const close = () => setOpen(null);
  const pick = (item: AddItem) => () => setOpen({ item, hostId: null });
  return (
    <>
      <Menu label="+ Add ▾" testId="infra-add" tip="Add a machine, an app target, a secret or an account" items={[
        { key: "host", label: "Host", testId: "infra-add-host", hidden: !pre.outwardSshAllowed,
          tip: "A machine BOSS reaches over SSH", onPick: pick("host") },
        { key: "enroll", label: "Enroll machine", testId: "infra-add-enroll", tip: "Run the hardware agent on a machine you have", onPick: pick("enroll") },
        { key: "target", label: "App target", testId: "infra-add-target", tip: "Where an app deploys", onPick: pick("target") },
        { key: "secret", label: "Secret", testId: "infra-add-secret", tip: "A key or token BOSS keeps by reference", onPick: pick("secret") },
        { key: "hwaccount", label: "Hardware account", testId: "infra-add-hwaccount", tip: "A provider account for machines", onPick: pick("hwaccount") },
      ]} />
      {open?.item === "host" ? (
        <HostSheet projectId={projectId} actor={actor} onClose={close}
                   onAdded={(id) => { close(); fleet.refresh(); go({ view: null, host: id, target: null }); }} />
      ) : null}
      {open?.item === "enroll" ? <EnrollSheet projectId={projectId} actor={actor} onClose={close} /> : null}
      {open?.item === "hwaccount" ? <HwAccountSheet projectId={projectId} actor={actor} onClose={close} /> : null}
      {open?.item === "secret" ? <SecretsSheet projectId={projectId} actor={actor} onClose={close} /> : null}
      {open?.item === "target" ? (
        <AppTargetSheet projectId={projectId} actor={actor} racks={fleet.data.racks} hostId={open.hostId} onClose={close}
                        onCreated={(id) => { close(); fleet.refresh(); if (id) go({ view: null, target: id, host: null }); }} />
      ) : null}
    </>
  );
}

// ── `Ask BOSS` (the Deployment Manager's starters, P-I-02) ──────────────────────────────────

/** The Deployment Manager panel's three starters, with a short label each (the full question
 *  goes to the dock and is the tooltip). About the fleet as a whole, so no record subject. */
export function fleetStarters(host: { name: string; address: string | null } | null): { label: string; question: string }[] {
  return [
    host
      ? { label: `Is ${host.name} healthy?`, question: `Is ${host.name}${host.address ? ` (${host.address})` : ""} healthy right now, and what is deployed on it?` }
      : { label: "Which hosts are healthy?", question: "What hosts can we deploy onto, and are they healthy?" },
    { label: "What shipped last?", question: "What shipped most recently, and did it pass its health check?" },
    host
      ? { label: `Deploy onto ${host.name}?`, question: `What would it take to deploy this project onto ${host.name}?` }
      : { label: "What would deploying take?", question: "What would it take to deploy this project?" },
  ];
}

export function InfraAskBoss({ fleet }: InfraTabProps & { fleet: Polled<Fleet> }) {
  const racks = fleet.data?.racks ?? [];
  const host = racks.find((r) => !r.thisComputer && r.state.tone === "green") ?? racks.find((r) => !r.thisComputer) ?? null;
  const ask = (question: string) => openDockAbout({ subject: null, question, role: "Deployment Manager", history: "push" });
  return (
    <Menu label="Ask BOSS" testId="infra-ask" tip="Ask the Deployment Manager about your machines and deploys" items={[
      ...fleetStarters(host).map((s, i) => ({ key: `s${i}`, label: s.label, testId: "infra-ask-starter", tip: s.question, onPick: () => ask(s.question) })),
      { key: "open", label: "Ask anything", testId: "infra-ask-open", tip: "Open the Deployment Manager", onPick: () => ask("") },
    ]} />
  );
}
