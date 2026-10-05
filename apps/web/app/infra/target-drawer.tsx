"use client";

/**
 * The target drawer (spec §4.5, `target=<id>`): DEPLOY (Ref picker over the repo's branches, one
 * routed `Deploy`, `Deploy on merge`, `Paused`), WHERE, PREFLIGHT (`Check`, read-only), HEALTH
 * (the 24h strip, uptime, `Open in Observability ↗`), RECENT DEPLOYS, SECRETS (`Bind secret`),
 * and the `⋯` menu's `Move to…` and `Retire…`.
 *
 * EVERY CONTROL IS THE SERVER'S: a button shows only when `can` says it can work, and a control
 * the edition pauses is disabled with the server's tooltip (`Paused in Community`). The old card
 * printed "Host removal unavailable until the approval flow is ready"; now Retire is offered when
 * the route allows it and is otherwise simply absent.
 */
import React, { useState } from "react";

import { deployHref, buildHref, useTargetDetail, type DeployRow, type Rack, type TargetDetail } from "../../lib/infra-view";
import { PulseStrip } from "../observability/pulse-strip";
import { navigate } from "../agents/live-util";
import { showToast } from "../client-forms";
import {
  afterWrite, bindSecret, deployTarget, listRefs, moveTarget, preflightTarget, retireTarget, setTargetToggles,
  type PreflightBody,
} from "./infra-actions";
import { ago, copyText, dur, ErrorLine, localTime, Menu, useNow } from "./infra-util";
import { confirmed, Drawer, DrawerSection, Empty, Skeleton, Sheet, StateWord, TypedConfirm } from "./kit";
import { Approver, StepDots, triggerWord } from "./deploy-row";
import { useSecrets } from "./secrets-sheet";

const CELL_S = 45 * 60;

/** `Up · 99.4% · 24h` — a share of stored probes, never progress. */
export function uptimeLine(t: Pick<TargetDetail, "health" | "uptime24h">): string {
  if (t.uptime24h === null) return `${t.health.word} · 24h`;
  const pct = t.uptime24h * 100;
  return `${t.health.word} · ${pct >= 99.95 && pct < 100 ? "99.9" : pct.toFixed(1)}% · 24h`;
}

/** One compact deploy row (RECENT DEPLOYS): the rail's pieces — state, sha, trigger, approver,
 *  step dots, duration, age — in the drawer's width; opens the deploy drawer. */
function RecentRow({ d, now, onOpen }: { d: DeployRow; now: number | null; onOpen: (ref: string) => void }) {
  return (
    <li>
      <button type="button" className="inf-row-btn" data-testid={`inf-target-recent-${d.ref}`} onClick={() => onOpen(d.ref)}
              title={[d.refName, d.startedAt ? localTime(d.startedAt) : null].filter(Boolean).join(" · ") || undefined}>
        <StateWord w={{ word: d.word, tone: d.tone }} />
        <span className="inf-mono" data-copy="data">{d.sha7 ?? "—"}</span>
        <span className="inf-muted">{triggerWord(d)}</span>
        <Approver approver={d.approver} />
        <span className="ag-spacer" />
        <StepDots steps={d.steps} />
        {d.durationMs !== null ? <span className="inf-num inf-faint">{dur(d.durationMs)}</span> : null}
        <span className="inf-num inf-faint">{d.startedAt && now !== null ? ago(d.startedAt, now) : ""}</span>
      </button>
    </li>
  );
}

function Preflight({ projectId, targetId, can }: { projectId: string; targetId: string; can: boolean }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PreflightBody | null>(null);
  const [error, setError] = useState<string | null>(null);
  const check = async () => {
    setBusy(true); setError(null);
    const r = await preflightTarget(projectId, targetId);
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    setResult(r.data);
  };
  return (
    <DrawerSection label="PREFLIGHT" testId="inf-target-preflight" actions={can ? (
      <button type="button" className="ag-btn" data-testid="inf-target-check" disabled={busy}
              title="Read-only checks: credentials, host, ports. Nothing deploys." onClick={() => void check()}>
        {busy ? "Checking…" : "Check"}
      </button>
    ) : undefined}>
      {result ? (
        <ul className="inf-checks" data-testid="inf-target-preflight-rows">
          {result.ready ? <li data-ok="1"><span className="inf-check-mark" aria-label="Passed">✓</span><span>Ready to deploy</span></li> : null}
          {(result.problems ?? []).map((p, i) => (
            <li key={i} data-ok="0"><span className="inf-check-mark" aria-label="Failed">✗</span><span className="inf-trunc" data-copy="data" title={p}>{p}</span></li>
          ))}
          {!result.ready && !(result.problems ?? []).length ? <li data-ok="0"><span className="inf-check-mark" aria-label="Failed">✗</span><span>Not ready</span></li> : null}
        </ul>
      ) : <span className="inf-muted">Not checked</span>}
      <ErrorLine error={error} />
    </DrawerSection>
  );
}

function Secrets({ projectId, actor, t, canBind, refresh }: { projectId: string; actor: string; t: TargetDetail; canBind: boolean; refresh: () => void }) {
  const [open, setOpen] = useState(false);
  const { secrets } = useSecrets(open ? projectId : "");
  const [name, setName] = useState("ssh_key");
  const [secretId, setSecretId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <DrawerSection label="SECRETS" testId="inf-target-secrets" actions={canBind && !open ? (
      <button type="button" className="ag-btn" data-testid="inf-target-bind" onClick={() => setOpen(true)}
              title="Give this target a stored secret by name">Bind secret</button>
    ) : undefined}>
      {t.secrets.length ? (
        <ul className="inf-checks">
          {t.secrets.map((s) => (
            <li key={s.name} data-ok={s.status === "active" ? "1" : "0"}>
              <span className="inf-mono" data-copy="data">{s.name}</span>
              <span className="inf-muted">{s.kind}</span>
              <span className="ag-spacer" />
              <span>{s.status}</span>
            </li>
          ))}
        </ul>
      ) : <span className="inf-muted">None bound</span>}
      {open ? (
        <form className="inf-form" data-testid="inf-target-bind-form" onSubmit={async (e) => {
          e.preventDefault();
          if (!name.trim() || !secretId) { setError("Name and secret"); return; }
          setBusy(true); setError(null);
          const r = await bindSecret(projectId, t.id, actor, name.trim(), secretId);
          setBusy(false);
          if (!r.ok) { setError(r.error); return; }
          showToast("success", "Secret bound");
          setOpen(false);
          afterWrite(refresh);
        }}>
          <div className="inf-form-row">
            <label className="inf-field"><span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" spellCheck={false} data-testid="inf-target-bind-name" /></label>
            <label className="inf-field"><span>Secret</span>
              <select value={secretId} onChange={(e) => setSecretId(e.target.value)} data-testid="inf-target-bind-secret">
                <option value="">Choose…</option>
                {secrets.map((s) => <option key={s.secret_ref_id} value={s.secret_ref_id}>{s.name.split("/").pop()} · {s.secret_kind}</option>)}
              </select></label>
          </div>
          <div className="inf-actions">
            <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-target-bind-submit">Bind</button>
            <button type="button" className="inf-link" onClick={() => setOpen(false)}>Cancel</button>
            <ErrorLine error={error} />
          </div>
        </form>
      ) : null}
    </DrawerSection>
  );
}

function MoveSheet({ t, projectId, actor, racks, onClose, onMoved }: {
  t: TargetDetail; projectId: string; actor: string; racks: Rack[]; onClose: () => void; onMoved: () => void;
}) {
  const choices = racks.filter((r) => r.state.tone === "green" && r.id !== t.where.host?.id && r.id !== "this");
  const [hostId, setHostId] = useState(choices[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={`Move ${t.app} · ${t.env}`} testId="inf-move-sheet" onClose={onClose}>
      {choices.length ? (
        <form className="inf-form" onSubmit={async (e) => {
          e.preventDefault();
          const to = choices.find((c) => c.id === hostId);
          if (!to) return;
          setBusy(true); setError(null);
          const r = await moveTarget(projectId, t.id, actor, to.id);
          setBusy(false);
          if (!r.ok) { setError(r.error); return; }
          showToast("success", `Moved to ${to.name}`);
          afterWrite();
          onMoved();
        }}>
          <label className="inf-field"><span>Machine</span>
            <select value={hostId} onChange={(e) => setHostId(e.target.value)} data-testid="inf-move-host">
              {choices.map((c) => <option key={c.id} value={c.id}>{c.name}{c.address ? ` · ${c.address}` : ""}</option>)}
            </select></label>
          <div className="inf-actions">
            <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-move-submit">Move</button>
            <button type="button" className="inf-link" onClick={onClose}>Cancel</button>
            <ErrorLine error={error} />
          </div>
        </form>
      ) : <Empty>No ready machine to move to</Empty>}
    </Sheet>
  );
}

function RetireTargetSheet({ t, actor, onClose, onRetired }: { t: TargetDetail; actor: string; onClose: () => void; onRetired: () => void }) {
  const name = t.app;
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={`Retire ${t.app} · ${t.env}?`} testId="inf-target-retire-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={async (e) => {
        e.preventDefault();
        if (!confirmed(name, typed)) return;
        setBusy(true); setError(null);
        const r = await retireTarget(t.id, actor);
        setBusy(false);
        if (!r.ok) { setError(r.error); return; }
        showToast("success", "Target retired");
        afterWrite();
        onRetired();
      }}>
        <span className="inf-muted" title="Its deploy history and health samples are kept">History is kept</span>
        <TypedConfirm name={name} value={typed} onChange={setTyped} testId="inf-target-retire-confirm" />
        <div className="inf-actions">
          <button type="submit" className="ag-btn" data-tone="reject" disabled={!confirmed(name, typed) || busy}
                  data-testid="inf-target-retire-submit">{busy ? "Retiring…" : "Retire"}</button>
          <button type="button" className="inf-link" onClick={onClose}>Cancel</button>
          <ErrorLine error={error} />
        </div>
      </form>
    </Sheet>
  );
}

function DeploySection({ t, projectId, actor, canWrite, now, refresh, onDeploy }: {
  t: TargetDetail; projectId: string; actor: string; canWrite: boolean; now: number | null; refresh: () => void;
  onDeploy: (ref: string) => void;
}) {
  const [ref, setRef] = useState(t.where.ref ?? "");
  const [refs, setRefs] = useState<{ name: string; sha?: string }[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRefs = async () => {
    if (refs !== null || !t.where.repo || !t.where.repo.includes("/")) return;
    setRefs([]);
    const r = await listRefs(projectId, t.where.repo);
    if (r.ok) {
      setRefs(r.data.branches ?? []);
      if (!ref && r.data.default_branch) setRef(r.data.default_branch);
    }
  };
  const deploy = async () => {
    setBusy("deploy"); setError(null);
    const r = await deployTarget(projectId, t.id, actor, ref.trim() || null);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Deploy planned");
    afterWrite(refresh);
    if (r.data.ref) onDeploy(r.data.ref);
  };
  const toggle = async (patch: { auto_deploy?: boolean; paused?: boolean }) => {
    setBusy("toggle"); setError(null);
    const r = await setTargetToggles(t.id, actor, patch);
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    afterWrite(refresh);
  };
  const last = t.recent.find((d) => d.word === "Live") ?? t.recent.find((d) => d.finishedAt) ?? null;
  const deployOff = t.disabled.deploy;
  const autoOff = t.disabled.auto_deploy ?? (!t.can.auto_deploy ? "You can't change this" : undefined);
  return (
    <DrawerSection label="DEPLOY" testId="inf-target-deploy-sec">
      <div className="inf-last-deploy" data-testid="inf-target-last-deploy">
        {last ? (
          <>
            <a className="inf-link inf-mono" href={deployHref(last.ref)} data-copy="data" title={`Deployed ${last.sha7 ?? "—"} · ${localTime(last.finishedAt)} · ${last.word}`}
               onClick={(e) => { e.preventDefault(); onDeploy(last.ref); }}>{last.sha7 ?? "—"} ↗</a>
            <StateWord w={{ word: last.word, tone: last.tone }} />
            {last.finishedAt && now !== null ? <span className="inf-faint">{ago(last.finishedAt, now)}</span> : null}
            {last.runId ? <a className="inf-link" href={buildHref(last.runId)} title="Open the build that made this"
                             onClick={(e) => { e.preventDefault(); navigate({ area: "code_tasks", build: last.runId, view: null }); }}>Build ↗</a> : null}
          </>
        ) : <span className="inf-muted" title="This target has no deploy yet">Never deployed</span>}
        {t.where.publicUrl ? <a className="inf-link" href={t.where.publicUrl} target="_blank" rel="noreferrer" data-testid="inf-target-open-app">Open app ↗</a> : null}
      </div>
      {canWrite ? (
        <>
          <div className="inf-actions">
            <label className="inf-inline-field" title="A branch, a tag, or a commit">
              <span>Ref</span>
              <input value={ref} onChange={(e) => setRef(e.target.value)} onFocus={() => void loadRefs()} list={`inf-refs-${t.id}`}
                     placeholder="main" autoComplete="off" spellCheck={false} data-testid="inf-target-ref" />
              <datalist id={`inf-refs-${t.id}`}>
                {(refs ?? []).map((r) => <option key={r.name} value={r.name}>{r.sha ?? ""}</option>)}
              </datalist>
            </label>
            {t.can.deploy || deployOff ? (
              <button type="button" className="ag-btn ag-btn-primary" data-testid="inf-target-deploy"
                      disabled={!!deployOff || busy !== null || !t.can.deploy}
                      title={deployOff ?? "Plans a deploy of this ref; you approve the plan"} onClick={() => void deploy()}>
                {busy === "deploy" ? "Planning…" : "Deploy"}
              </button>
            ) : null}
          </div>
          <div className="inf-actions">
            <label className="inf-check" title={autoOff ?? "Plan a deploy each time the repo's main branch moves"}>
              <input type="checkbox" checked={t.toggles.autoDeploy} disabled={!!autoOff || busy !== null} data-testid="inf-target-auto"
                     onChange={(e) => void toggle({ auto_deploy: e.target.checked })} /> Deploy on merge
            </label>
            <label className="inf-check" title="No deploys to this target until unpaused">
              <input type="checkbox" checked={t.toggles.paused} disabled={busy !== null} data-testid="inf-target-paused"
                     onChange={(e) => void toggle({ paused: e.target.checked })} /> Paused
            </label>
          </div>
        </>
      ) : null}
      <ErrorLine error={error} testId="inf-target-error" />
    </DrawerSection>
  );
}

export function TargetDrawer({ projectId, actor, targetId, canWrite, racks, onClose, go }: {
  projectId: string; actor: string; targetId: string; canWrite: boolean; racks: Rack[];
  onClose: () => void; go: (patch: Record<string, string | null>, mode?: "push" | "replace") => void;
}) {
  const detail = useTargetDetail(projectId, targetId);
  const now = useNow(30_000);
  const [sheet, setSheet] = useState<"move" | "retire" | null>(null);
  const t = detail.data;
  const openDeploy = (ref: string) => go({ view: "deploys", target: null, host: null, deploy: ref });
  const label = t ? `${t.app} · ${t.env}` : "App";
  const end = now ?? NaN;   // the clock starts after mount
  const head = t ? (
    <>
      <span className="inf-drawer-title inf-trunc" data-copy="data">{label}</span>
      <StateWord w={t.health} testId="inf-target-state" />
      <Menu label="⋯" testId="inf-target-more" tip="More" items={[
        { key: "copy", label: "Copy link", testId: "inf-target-copy-link",
          onPick: () => void copyText(`${window.location.origin}/?area=infra&view=fleet&target=${encodeURIComponent(t.id)}`) },
        { key: "move", label: "Move to…", testId: "inf-target-move", hidden: !(canWrite && t.can.move), onPick: () => setSheet("move") },
        { key: "retire", label: "Retire…", testId: "inf-target-retire", hidden: !(canWrite && t.can.retire), onPick: () => setSheet("retire") },
      ]} />
    </>
  ) : <span className="inf-drawer-title">App</span>;
  return (
    <Drawer label={label} testId="inf-target-drawer" onClose={onClose} head={head}>
      {!t && detail.loading ? <Skeleton testId="inf-target-loading" /> : null}
      {!t && detail.error ? <Empty testId="inf-target-missing"><span title={detail.error}>Can&apos;t read this app</span></Empty> : null}
      {t ? (
        <>
          <DeploySection key={t.id} t={t} projectId={projectId} actor={actor} canWrite={canWrite} now={now}
                         refresh={detail.refresh} onDeploy={openDeploy} />
          <DrawerSection label="WHERE" testId="inf-target-where">
            <dl className="inf-facts">
              <dt>Host</dt><dd>{t.where.host ? (
                <a className="inf-link" href={`?area=infra&view=fleet&host=${encodeURIComponent(t.where.host.id)}`} data-copy="data"
                   onClick={(e) => { e.preventDefault(); go({ target: null, host: t.where.host!.id }); }}>{t.where.host.name} ↗</a>
              ) : "—"}</dd>
              <dt>Kind</dt><dd>{t.kind || "—"}</dd>
              <dt>Ref</dt><dd className="inf-mono" data-copy="data">{t.where.ref ?? "—"}</dd>
              <dt>Repo</dt><dd className="inf-mono" data-copy="data">{t.where.repo ?? "—"}</dd>
              <dt>Manifest</dt><dd className="inf-mono" data-copy="data">{t.where.manifest ?? "—"}</dd>
              <dt>Domain</dt><dd data-testid="inf-target-domain" data-copy="data">{t.where.domain
                ? (t.where.publicUrl ? <a className="inf-link" href={t.where.publicUrl} target="_blank" rel="noreferrer">{t.where.domain} ↗</a> : t.where.domain)
                : t.where.publicUrl ? <a className="inf-link" href={t.where.publicUrl} target="_blank" rel="noreferrer">{t.where.publicUrl} ↗</a> : "—"}</dd>
              {t.where.account ? (
                <><dt>Account</dt><dd>
                  <a className="inf-link" href={`?area=infra&view=cloud&cloud=accounts&account=${encodeURIComponent(t.where.account.id)}`} data-copy="data"
                     onClick={(e) => { e.preventDefault(); go({ view: "cloud", cloud: "accounts", target: null, account: t.where.account!.id }); }}>
                    {t.where.account.name} ↗</a>
                </dd></>
              ) : null}
            </dl>
          </DrawerSection>
          <Preflight projectId={projectId} targetId={t.id} can={t.can.check} />
          <DrawerSection label="HEALTH" testId="inf-target-health">
            <PulseStrip cells={t.strip} fromIso={Number.isFinite(end) ? new Date(end - t.strip.length * CELL_S * 1000).toISOString() : null} cellS={CELL_S}
                        label={label} span="24 hours" testId="inf-target-strip" />
            <div className="inf-actions">
              <span className="inf-num" data-testid="inf-target-uptime">{uptimeLine(t)}</span>
              <span className="ag-spacer" />
              <a className="inf-link" href={`?area=observability&view=apps&app=${encodeURIComponent(t.id)}`} data-testid="inf-target-obs"
                 onClick={(e) => { e.preventDefault(); navigate({ area: "observability", view: "apps", app: t.id, target: null }); }}>
                Open in Observability ↗</a>
            </div>
          </DrawerSection>
          <DrawerSection label="RECENT DEPLOYS" testId="inf-target-recent">
            {t.recent.length ? (
              <ul className="inf-resident-rows">
                {t.recent.slice(0, 5).map((d) => <RecentRow key={d.ref} d={d} now={now} onOpen={openDeploy} />)}
              </ul>
            ) : <span className="inf-muted">No deploys yet</span>}
          </DrawerSection>
          <Secrets projectId={projectId} actor={actor} t={t} canBind={canWrite} refresh={detail.refresh} />
          {sheet === "move" ? <MoveSheet t={t} projectId={projectId} actor={actor} racks={racks} onClose={() => setSheet(null)}
                                         onMoved={() => { setSheet(null); detail.refresh(); }} /> : null}
          {sheet === "retire" ? <RetireTargetSheet t={t} actor={actor} onClose={() => setSheet(null)}
                                                   onRetired={() => { setSheet(null); onClose(); }} /> : null}
        </>
      ) : null}
    </Drawer>
  );
}
