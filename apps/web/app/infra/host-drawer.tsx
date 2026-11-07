"use client";

/**
 * The host drawer (spec §4.4, `host=<id>`): SETUP while the machine is not Ready — the old
 * FleetPanel wizard, every stage's controls kept, labels shortened (§5.1) with the old long
 * labels as tooltips — then FACTS, CHECKS, ON THIS MACHINE and RETIRE behind a typed-name
 * confirm (the retire the old panel did with no confirm at all). This computer (`host=this`)
 * shows its facts from hardware_places and `Correct record`; it has no retire.
 *
 * THE STAGE IS THE SERVER'S (infra_view.host_stage, the old hostStage ported), and only the
 * current stage opens by default: Generate key on a machine that already has one mints a new
 * key and invalidates the authorise command the operator may be part-way through running, so
 * a finished stage's controls stay one press further away, with that warning as its tooltip.
 *
 * ONE GATE PER ACTION (infra-offers-only-host-actions-that-can-work): an action that reaches
 * the machine shows only when the server says it can work (`can.verify` is false while outward
 * SSH is off); host writes only when `can.install_key` / `can.install_docker` / `can.retire`.
 * Never a dead button.
 */
import { consoleHref } from "../console-href";
import { GatedButton } from "../../components/gated";
import React, { useEffect, useState } from "react";

import { registerLocalPlace } from "../../lib/hardware";
import { withKeyFile } from "../../lib/ssh-key-file";
import { STAGES, useHostDetail, type HostDetail, type Resident, type StageKey } from "../../lib/infra-view";
import { showToast } from "../client-forms";
import {
  adoptHostKey, afterWrite, generateHostKey, installHostKey, provisionHost, retireHost, verifyHost,
} from "./infra-actions";
import { ago, copyText, CopyButton, ErrorLine, localTime, Menu, useNow } from "./infra-util";
import { confirmed, Drawer, DrawerSection, Empty, Skeleton, Sheet, StateWord, TypedConfirm } from "./kit";
import { StageLine } from "./rack";
import { ServerOverview } from "./server-overview";
import { setCrumbTrail } from "../shell-crumb-here";

const STORES: { key: string; label: string; tip: string }[] = [
  { key: "database", label: "Database", tip: "database (survives restarts)" },
  { key: "file", label: "File", tip: "file (local disk — lost on redeploy)" },
  { key: "platform_secret_store", label: "Secret store", tip: "platform_secret_store" },
];

type Busy = string | null;

function useBusy(): [Busy, <T>(key: string, fn: () => Promise<T>) => Promise<T | undefined>] {
  const [busy, setBusy] = useState<Busy>(null);
  const run = async <T,>(key: string, fn: () => Promise<T>): Promise<T | undefined> => {
    if (busy) return undefined;
    setBusy(key);
    try { return await fn(); } finally { setBusy(null); }
  };
  return [busy, run];
}

/** A fact in words. A label set is an object, and String() of it printed "[object Object]"
 *  in the host drawer: it reads `role ci · region fsn1` now, and an empty one is "—". */
export const fact = (v: unknown): string => {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map(fact).join(", ") : "—";
  if (typeof v === "object") {
    const pairs = Object.entries(v as Record<string, unknown>)
      .filter(([, x]) => x !== null && x !== undefined && x !== "")
      .map(([k, x]) => (x === true ? k : `${k} ${fact(x)}`));
    return pairs.length ? pairs.join(" · ") : "—";
  }
  return String(v);
};

// ── SETUP ───────────────────────────────────────────────────────────────────────────────────

/** One stage of the vertical stage line: open when it is the current one; done ✓; a later one
 *  stays reachable but closed. */
function StepBox({ k, index, current, children }: { k: StageKey; index: number; current: StageKey; children: React.ReactNode }) {
  const at = STAGES.findIndex((s) => s.key === k) + 1;
  const state = at < index ? "done" : at === index ? "current" : "todo";
  const label = STAGES[at - 1]?.label ?? k;
  return (
    <details className="inf-setup-step" data-state={state} open={k === current} data-testid={`inf-step-${k}`}>
      <summary>
        <span className="inf-stage-mark" aria-hidden>{state === "done" ? "✓" : at}</span>
        <span>{label}</span>
        {state === "current" ? <span className="inf-setup-step-now">now</span> : null}
      </summary>
      <div className="inf-setup-step-body">{children}</div>
    </details>
  );
}

function Setup({ host, actor, refresh }: { host: HostDetail; actor: string; refresh: () => void }) {
  const [busy, run] = useBusy();
  const [error, setError] = useState<string | null>(null);
  const [store, setStore] = useState("database");
  const [adopting, setAdopting] = useState(false);
  const [privateKey, setPrivateKey] = useState("");
  const [route, setRoute] = useState(host.routeOptions[0]?.key ?? "");
  const current = host.stage.stage;
  const can = host.can;
  const [keyFile, setKeyFile] = useState("");
  const shown = host.routeOptions.find((r) => r.key === route)?.command ?? host.installCommand ?? "";
  const command = withKeyFile(shown, keyFile);
  const address = String(host.facts.address ?? host.name);

  const act = (key: string, fn: () => Promise<{ ok: boolean; error?: string }>, done?: string) => run(key, async () => {
    setError(null);
    const r = await fn();
    if (!r.ok) { setError(r.error ?? "Couldn't do that"); return; }
    if (done) showToast("success", done);
    afterWrite(refresh);
  });
  const verify = (key: string, label: string) => act(key, async () => {
    const r = await verifyHost(host.id, actor, label);
    return r.ok ? r : { ok: false, error: r.error };
  }, "Verified");

  const redo = "Mints a new key; any authorise command already copied stops working";

  return (
    <DrawerSection label="SETUP" testId="inf-host-setup">
      <StageLine stage={host.stage} vertical testId="inf-host-stage-line" />
      <StepBox index={host.stage.index} current={current} k="credential">
        <div className="inf-actions">
          {can.adopt_key ? (
            <GatedButton permission="host:manage" type="button" className="ag-btn" data-testid="inf-stage-adopt" aria-expanded={adopting}
                    title="Paste a private key that already reaches this machine"
                    onClick={() => setAdopting((v) => !v)}>Use my key</GatedButton>
          ) : null}
          {can.generate_key ? (
            <GatedButton permission="host:manage" type="button" className={current === "credential" ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="inf-stage-generate"
                    disabled={busy !== null}
                    title={current === "credential" ? "BOSS makes a key pair and shows the line to authorise" : redo}
                    onClick={() => void act("generate", () => generateHostKey(host.id, actor, store))}>
              {busy === "generate" ? "Generating…" : "Generate key"}
            </GatedButton>
          ) : null}
          <label className="inf-inline-field" title="Where BOSS keeps the private half of this machine's key">
            <span>Store in</span>
            <select value={store} onChange={(e) => setStore(e.target.value)} data-testid="inf-stage-store">
              {STORES.map((s) => <option key={s.key} value={s.key} title={s.tip}>{s.label}</option>)}
            </select>
          </label>
        </div>
        {adopting ? (
          <form className="inf-form" onSubmit={(e) => {
            e.preventDefault();
            if (!privateKey.trim()) { setError("Paste the private key"); return; }
            void act("adopt", () => adoptHostKey(host.id, actor, privateKey, store), "Key adopted").then(() => { setPrivateKey(""); setAdopting(false); });
          }}>
            <label className="inf-field" title={`The private key that already works on ${address}. BOSS keeps a reference and a fingerprint, never the key.`}>
              <span>Private key</span>
              <textarea value={privateKey} onChange={(e) => setPrivateKey(e.target.value)} rows={4} spellCheck={false}
                        placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" data-testid="inf-stage-adopt-key" />
            </label>
            <div className="inf-actions">
              <GatedButton permission="host:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-stage-adopt-submit">Adopt key</GatedButton>
            </div>
          </form>
        ) : null}
      </StepBox>
      <StepBox index={host.stage.index} current={current} k="install">
        {host.keyPublic ? (
          <div className="inf-keyblock" data-testid="inf-stage-key">
            <code className="inf-mono inf-code" data-copy="data" data-testid="inf-stage-public-key">{host.keyPublic}</code>
            <div className="inf-actions">
              <CopyButton text={host.keyPublic} label="Copy key" testId="inf-stage-copy-key" tip="Copy public key" />
            </div>
            {host.authoriseLine ? (
              <>
                <code className="inf-mono inf-code" data-copy="data" data-testid="inf-stage-authorise-line"
                      title={`Run on ${address} as ${fact(host.facts.user)}`}>{host.authoriseLine}</code>
                <div className="inf-actions">
                  <CopyButton text={host.authoriseLine} label="Copy line" testId="inf-stage-copy-line"
                              tip="Adds the key to authorized_keys, typed on the machine" />
                </div>
              </>
            ) : null}
          </div>
        ) : <span className="inf-muted">No key yet</span>}
        {host.routeOptions.length ? (
          <label className="inf-inline-field" title={`How will you reach ${address}?`}>
            <span>Reach it by</span>
            <select value={route} onChange={(e) => setRoute(e.target.value)} data-testid="inf-stage-route">
              {host.routeOptions.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
            </select>
          </label>
        ) : null}
        {/^ssh\s/.test(shown) ? (
          <>
            <div className="inf-muted" data-testid="inf-stage-key-note">
              This uses your laptop&apos;s default SSH key. If another key opens {address}, name its file.
            </div>
            <label className="inf-inline-field" title="Adds -i <file> to the command below">
              <span>Key file</span>
              <input value={keyFile} onChange={(e) => setKeyFile(e.target.value)} spellCheck={false}
                     placeholder="~/.ssh/id_ed25519 (optional)" data-testid="inf-stage-key-file" />
            </label>
          </>
        ) : null}
        {command ? <code className="inf-mono inf-code" data-copy="data" data-testid="inf-stage-command">{command}</code> : null}
        <div className="inf-actions">
          {command ? <CopyButton text={command} label="Copy command" testId="inf-stage-copy-command" /> : null}
          {can.verify ? (
            <GatedButton permission="host:manage" type="button" className={current === "install" ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="inf-stage-ran"
                    disabled={busy !== null} title="Checks BOSS can now reach the machine (I've run it — verify)"
                    onClick={() => void verify("ran", "I ran it")}>{busy === "ran" ? "Connecting…" : "I ran it"}</GatedButton>
          ) : null}
        </div>
      </StepBox>
      <StepBox index={host.stage.index} current={current} k="verify">
        <div className="inf-actions">
          {can.verify ? (
            <GatedButton permission="host:manage" type="button" className={current === "verify" ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="inf-stage-verify"
                    disabled={busy !== null} title="Opens a real SSH session to the machine"
                    onClick={() => void verify("verify", "Verify")}>{busy === "verify" ? "Connecting…" : "Verify"}</GatedButton>
          ) : null}
          {can.install_key ? (
            <GatedButton permission="host:manage" type="button" className="ag-btn" data-testid="inf-stage-install-key" disabled={busy !== null}
                    title="BOSS logs in once with your key and installs its own"
                    onClick={() => void act("install-key", () => installHostKey(host.id, actor, store))}>
              {busy === "install-key" ? "Installing…" : "Install key for me"}
            </GatedButton>
          ) : null}
        </div>
      </StepBox>
      <StepBox index={host.stage.index} current={current} k="provision">
        <div className="inf-actions">
          {can.install_docker ? (
            <GatedButton permission="host:manage" type="button" className={current === "provision" ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="inf-stage-docker"
                    disabled={busy !== null} title="Install docker on the machine; BOSS ships apps as containers"
                    onClick={() => void act("docker", () => provisionHost(host.id, actor))}>
              {busy === "docker" ? "Installing…" : "Install Docker"}
            </GatedButton>
          ) : null}
          {can.verify ? (
            <GatedButton permission="host:manage" type="button" className="ag-btn" data-testid="inf-stage-verify-provision" disabled={busy !== null}
                    onClick={() => void verify("verify-2", "Verify")}>Verify</GatedButton>
          ) : null}
        </div>
      </StepBox>
      <ErrorLine error={error} testId="inf-host-error" />
    </DrawerSection>
  );
}

// ── This computer: Correct record ───────────────────────────────────────────────────────────

function CorrectRecord({ host, projectId, actor, refresh }: { host: HostDetail; projectId: string; actor: string; refresh: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!host.can.correct) return null;
  if (!open) {
    return (
      <GatedButton permission="host:manage" type="button" className="ag-btn" data-testid="inf-host-correct" title="Correct this computer's record"
              onClick={() => setOpen(true)}>Correct record</GatedButton>
    );
  }
  return (
    <form className="inf-form" data-testid="inf-host-correct-form" onSubmit={async (e) => {
      e.preventDefault();
      const f = new FormData(e.currentTarget);
      setBusy(true); setError(null);
      try {
        await registerLocalPlace({ project_id: projectId, actor, hostname: String(f.get("hostname") ?? "").trim(),
                                   arch: String(f.get("arch") ?? "").trim(), os: String(f.get("os") ?? "").trim() });
        showToast("success", "Record saved");
        setOpen(false);
        afterWrite(refresh);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally { setBusy(false); }
    }}>
      <div className="inf-form-row">
        <label className="inf-field"><span>Hostname</span><input name="hostname" required defaultValue={fact(host.facts.hostname).replace("—", "")} autoComplete="off" /></label>
        <label className="inf-field"><span>Arch</span><input name="arch" required defaultValue={fact(host.facts.arch).replace("—", "")} placeholder="arm64" autoComplete="off" /></label>
        <label className="inf-field"><span>OS</span><input name="os" required defaultValue={fact(host.facts.os).replace("—", "")} placeholder="linux" autoComplete="off" /></label>
      </div>
      <div className="inf-actions">
        <GatedButton permission="host:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-host-correct-save">Save</GatedButton>
        <button type="button" className="inf-link" onClick={() => setOpen(false)}>Cancel</button>
        <ErrorLine error={error} />
      </div>
    </form>
  );
}

// ── RETIRE: four choices and the typed name ─────────────────────────────────────────────────

export function RetireHostSheet({ host, projectId, actor, onClose, onRetired }: {
  host: HostDetail; projectId: string; actor: string; onClose: () => void; onRetired: () => void;
}) {
  const apps = host.residents.filter((r) => r.kind === "target").length;
  const [typed, setTyped] = useState("");
  const [revoke, setRevoke] = useState(true);
  const [docker, setDocker] = useState(false);
  const [purge, setPurge] = useState(false);
  const [detach, setDetach] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = confirmed(host.name, typed);
  return (
    <Sheet title={`Retire ${host.name}?`} testId="inf-retire-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={async (e) => {
        e.preventDefault();
        if (!ok) return;
        setBusy(true); setError(null);
        const r = await retireHost(projectId, host.id, actor, { confirm_name: typed.trim(), revoke_key: revoke,
          uninstall_docker: docker, delete_record: purge, detach_apps: detach });
        setBusy(false);
        if (!r.ok) { setError(r.error); return; }
        showToast("success", "Host retired");
        afterWrite();
        onRetired();
      }}>
        <label className="inf-check" title="Removes only the keys BOSS put there; a key you lent BOSS stays">
          <input type="checkbox" checked={revoke} onChange={(e) => setRevoke(e.target.checked)} data-testid="inf-retire-revoke" /> Revoke BOSS key
        </label>
        <label className="inf-check" title="Only if nothing else on this machine uses docker">
          <input type="checkbox" checked={docker} onChange={(e) => setDocker(e.target.checked)} data-testid="inf-retire-docker" /> Uninstall Docker
        </label>
        <label className="inf-check" title="Otherwise it is kept, marked retired, for the audit trail">
          <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} data-testid="inf-retire-purge" /> Delete record
        </label>
        {apps ? (
          <label className="inf-check" title="The apps still pointed here lose their machine">
            <input type="checkbox" checked={detach} onChange={(e) => setDetach(e.target.checked)} data-testid="inf-retire-detach" /> Detach {apps} {apps === 1 ? "app" : "apps"}
          </label>
        ) : null}
        <TypedConfirm name={host.name} value={typed} onChange={setTyped} testId="inf-retire-confirm" />
        <div className="inf-actions">
          <GatedButton permission="host:manage" type="submit" className="ag-btn" data-tone="reject" disabled={!ok || busy} data-testid="inf-retire-submit">
            {busy ? "Retiring…" : "Retire"}
          </GatedButton>
          <button type="button" className="inf-link" onClick={onClose}>Cancel</button>
          <ErrorLine error={error} testId="inf-retire-error" />
        </div>
      </form>
    </Sheet>
  );
}

// ── the drawer ──────────────────────────────────────────────────────────────────────────────

export function HostDrawer({ projectId, actor, hostId, canWrite, onClose, onResident }: {
  projectId: string; actor: string; hostId: string; canWrite: boolean;
  onClose: () => void; onResident: (r: Resident) => void;
}) {
  const detail = useHostDetail(projectId, hostId);
  const now = useNow(30_000);
  const [retiring, setRetiring] = useState(false);
  const [busy, run] = useBusy();
  const [error, setError] = useState<string | null>(null);
  const h = detail.data;
  // A drawer opened on a host that is gone (retired and deleted) says so rather than spinning.
  useEffect(() => { setError(null); }, [hostId]);
  // THE BREADCRUMB NAMES THE SERVER (14-01): "Infra / Servers / 203.0.113.20" while it is open —
  // a server is shared and org-wide, never "<project> / Infra".
  const crumbName = h ? String(h.facts.address ?? h.name) : "";
  useEffect(() => {
    if (!crumbName) return undefined;
    setCrumbTrail({ area: "infra", trail: [{ label: "Servers", href: "?area=infra&view=fleet" }, { label: crumbName }] });
    return () => setCrumbTrail(null);
  }, [crumbName]);

  const head = h ? (
    <>
      <span className="inf-drawer-title inf-trunc" data-copy="data">{h.thisComputer ? `This computer · ${h.name}` : h.name}</span>
      <StateWord w={h.state} testId="inf-host-state" />
      <Menu label="⋯" testId="inf-host-more" tip="More" items={[
        { key: "copy", label: "Copy address", testId: "inf-host-copy-address", hidden: !h.facts.address,
          onPick: () => void copyText(String(h.facts.address ?? "")) },
        { key: "retire", label: "Retire…", testId: "inf-host-more-retire", hidden: !(canWrite && h.can.retire),
          tip: "Stops BOSS using this machine. Choose what to clean up.", onPick: () => setRetiring(true) },
      ]} />
    </>
  ) : <span className="inf-drawer-title">Machine</span>;

  return (
    <Drawer label={h ? `Machine ${h.name}` : "Machine"} testId="inf-host-drawer" onClose={onClose} head={head} width={880} expandable>
      {!h && detail.loading ? <Skeleton testId="inf-host-loading" /> : null}
      {!h && detail.error ? <Empty testId="inf-host-missing"><span title={detail.error}>Can&apos;t read this machine</span></Empty> : null}
      {h ? (
        <>
          {/* THE SERVER AT A GLANCE (screen 14): capacity, the projects on it, plugins, changes. */}
          {!h.thisComputer ? <ServerOverview projectId={projectId} hostId={h.id} canExec={canWrite} /> : null}
          {canWrite && !h.thisComputer && h.stage.stage !== "ready" && h.state.tone !== "none"
            ? <Setup host={h} actor={actor} refresh={detail.refresh} /> : null}
          <DrawerSection label="FACTS" testId="inf-host-facts">
            {h.thisComputer ? (
              <dl className="inf-facts">
                <dt>Hostname</dt><dd data-copy="data">{fact(h.facts.hostname)}</dd>
                <dt>Arch</dt><dd>{fact(h.facts.arch)}</dd>
                <dt>OS</dt><dd>{fact(h.facts.os)}</dd>
                <dt>Agent</dt><dd>{h.facts.agent_state === "installed" ? "Installed" : "Not ready"}</dd>
              </dl>
            ) : (
              <dl className="inf-facts">
                <dt>Address</dt><dd className="inf-mono" data-copy="data">{fact(h.facts.address)}</dd>
                <dt>User</dt><dd className="inf-mono" data-copy="data">{fact(h.facts.user)}</dd>
                <dt>Port</dt><dd className="inf-num">{fact(h.facts.port)}</dd>
                <dt>Arch</dt><dd>{fact(h.facts.arch)}</dd>
                <dt>Labels</dt><dd data-copy="data">{fact(h.facts.labels)}</dd>
                <dt>Ports</dt><dd className="inf-num">{h.facts.port_min !== undefined && h.facts.port_min !== null
                  ? `${fact(h.facts.port_min)}–${fact(h.facts.port_max)}` : "—"}</dd>
                <dt>Max apps</dt><dd className="inf-num">{fact(h.facts.max_apps)}</dd>
                <dt>Key</dt><dd className="inf-key-fact">
                  <span className="inf-mono inf-trunc" data-copy="data" title={fact(h.facts.key_fingerprint)}>{fact(h.facts.key_fingerprint)}</span>
                  {h.facts.key_fingerprint ? <CopyButton text={String(h.facts.key_fingerprint)} label="Copy" testId="inf-host-copy-key" /> : null}
                </dd>
                {h.hostKey ? (
                  <>
                    <dt>Host key</dt>
                    <dd data-testid="inf-host-hostkey" title={h.hostKey.words}>
                      <span className={h.hostKey.pinned ? "inf-mono inf-trunc" : "inf-muted"} data-copy="data">
                        {h.hostKey.pinned ? h.hostKey.fingerprint : "Not pinned yet"}</span>
                      <span className="inf-muted"> — {h.hostKey.words}</span>
                    </dd>
                  </>
                ) : null}
              </dl>
            )}
            {canWrite && h.thisComputer ? <CorrectRecord host={h} projectId={projectId} actor={actor} refresh={detail.refresh} /> : null}
          </DrawerSection>
          {!h.thisComputer ? (
            <DrawerSection label="CHECKS" testId="inf-host-checks" actions={h.can.verify && canWrite ? (
              <GatedButton permission="host:manage" type="button" className="ag-btn" data-testid="inf-host-reverify" disabled={busy !== null}
                      title="Opens a real SSH session now" onClick={() => void run("reverify", async () => {
                        setError(null);
                        const r = await verifyHost(h.id, actor, "Re-verify");
                        if (!r.ok) { setError(r.error); return; }
                        showToast("success", "Verified");
                        afterWrite(detail.refresh);
                      })}>{busy === "reverify" ? "Connecting…" : "Re-verify"}</GatedButton>
            ) : undefined}>
              {h.findings.length ? (
                <ul className="inf-checks">
                  {h.findings.map((f, i) => (
                    <li key={`${f.kind}-${i}`} data-ok={f.ok ? "1" : "0"} data-copy="data" title={[f.detail, f.remedy].filter(Boolean).join(" · ") || undefined}>
                      <span className="inf-check-mark" aria-label={f.ok ? "Passed" : "Failed"}>{f.ok ? "✓" : "✗"}</span>
                      <span>{f.label}</span>
                      {!f.ok && f.remedy ? <span className="inf-muted inf-trunc" data-copy="data">{f.remedy}</span> : null}
                    </li>
                  ))}
                </ul>
              ) : <span className="inf-muted">Not checked yet</span>}
              {h.checkedAt && now !== null ? <span className="inf-faint" title={localTime(h.checkedAt)}>checked {ago(h.checkedAt, now)}</span> : null}
              <ErrorLine error={error} />
            </DrawerSection>
          ) : null}
          {h.thisComputer ? <ProjectsOnServer residents={h.residents} /> : null}
          <DrawerSection label="ON THIS MACHINE" testId="inf-host-residents">
            {h.residents.length ? (
              <ul className="inf-resident-rows">
                {h.residents.map((r) => (
                  <li key={`${r.kind}-${r.id}`}>
                    <button type="button" className="inf-row-btn" data-testid={`inf-host-resident-${r.id}`} onClick={() => onResident(r)}>
                      <span className="inf-trunc" data-copy="data">{r.kind === "target" && r.env ? `${r.name} · ${r.env}` : r.name}</span>
                      <StateWord w={r.health} />
                    </button>
                  </li>
                ))}
              </ul>
            ) : <span className="inf-muted">Nothing here yet</span>}
          </DrawerSection>
          {canWrite && h.can.retire && !h.thisComputer ? (
            <DrawerSection label="RETIRE" testId="inf-host-retire-sec">
              <div className="inf-actions">
                <GatedButton permission="host:manage" type="button" className="ag-btn" data-tone="reject" data-testid="inf-host-retire"
                        title="Stops BOSS using this machine. Choose what to clean up." onClick={() => setRetiring(true)}>Retire…</GatedButton>
              </div>
            </DrawerSection>
          ) : null}
          {retiring ? (
            <RetireHostSheet host={h} projectId={projectId} actor={actor} onClose={() => setRetiring(false)}
                             onRetired={() => { setRetiring(false); onClose(); }} />
          ) : null}
        </>
      ) : null}
    </Drawer>
  );
}

/** PROJECTS ON THIS SERVER (BOSSAPP-914 screen 14): a shared server stays in Infra; what runs on it
 *  is grouped by project, each linking to that Project › Deployments (which links back here). */
export function ProjectsOnServer({ residents }: { residents: HostDetail["residents"] }) {
  const byProject = new Map<string, { name: string; apps: HostDetail["residents"] }>();
  for (const r of residents) {
    if (!r.project) continue;
    const g = byProject.get(r.project.id) ?? { name: r.project.name, apps: [] };
    g.apps.push(r);
    byProject.set(r.project.id, g);
  }
  if (!byProject.size) return null;
  return (
    <DrawerSection label="PROJECTS ON THIS SERVER" testId="inf-host-projects">
      <ul className="inf-resident-rows">
        {[...byProject.entries()].map(([id, g]) => (
          <li key={id} className="inf-row-btn inf-host-project" data-testid={`inf-host-project-${id}`}>
            <span className="inf-trunc inf-host-project-name">
              <b>{g.name}</b>{" "}
              <span className="inf-faint">{g.apps.map((a) => [a.env, a.port ? `:${a.port}` : ""].filter(Boolean).join(" ")).filter(Boolean).join(" · ")}</span>
            </span>
            <StateWord w={g.apps[0].health} />
            <a className="inf-link" href={consoleHref({ project_id: id, area: "project", tab: "deployments", host: undefined, view: undefined })}
               data-console-load data-testid={`inf-host-project-link-${id}`} title={`${g.name}: its deployments, demo logins and health`}>
              Project › Deployments
            </a>
          </li>
        ))}
      </ul>
    </DrawerSection>
  );
}
