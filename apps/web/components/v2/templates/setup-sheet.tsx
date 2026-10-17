"use client";

/**
 * APP ▸ SETUP (BOSSAPP-568, the approved redesign): an app's provider-side setup, in two tabs.
 *
 *   Connectors  ONE CARD PER CONNECTOR (lib/app-connectors.ts dedupes the build attempts: a
 *               connector built twelve times is one card, the eleven older attempts folded). Its
 *               header says what works now and what is missing, and where (Preview · Deploy). A
 *               row per sign-in provider — Google, GitHub, BOSS, the demo login — with its status
 *               and what to do: the steps, the callback address to register (a warning only when
 *               it cannot work), the client id and secret, and where to use them. A connector
 *               with plain keys (Stripe) lists its keys the same way. The full guide stays folded.
 *   Settings    what BOSS manages (generated, a test double: read-only, demo passwords on Show)
 *               and what a person set (Edit, Remove, Add setting).
 *
 * Opened from the app row's chip ("Set up sign-in · N providers"), its ⋯ › Setup and ⋯ › Sign-in,
 * and the build page's Settings: one surface for the job.
 *
 * Values are WRITE-ONLY (§2.4): a field clears once stored; nothing ever reads one back. Writes
 * need a fresh second factor (app_secret:write is step-up): useGuardedCall opens the sheet. A
 * viewer sees every control greyed with the reason (components/gated.tsx). An address inside BOSS
 * (api:3001) is never offered as one to register.
 */
import React, { useCallback, useEffect, useId, useMemo, useState } from "react";

import { GatedButton, useGate } from "../../gated";
import { useGuardedCall } from "../../identity/identity-provider";
import { IdentityError } from "../../../lib/identity/api";
import { PUBLIC_URL_HOWTO } from "../../../lib/identity/public-url";
import { Markdown } from "../../../app/markdown";
import { BossSignInDetails, useBossSignIn } from "../../../app/factory/sign-in-sheet";
import {
  APPLIED_WORD, DEPLOY_VALUE_NAME, ENV_WORD, refusalWords, resolveAppKey, secretWord, stackLabel, templatesApi,
  type AppSecrets, type Applied, type DeclaredSecret, type DemoLogin, type Guide, type SecretBinding, type SetupAddresses,
} from "../../../lib/templates";
import {
  connectorCards, connectorKeys, demoLoginNames, isUsableAddress, providerCallbacks, providerRow, previewReachWords,
  settingsSplit, summaryLine, whereLine, type ConnectorCard, type ProviderRow, type SettingRow,
} from "../../../lib/app-connectors";
import { CopyValue, SideSheet } from "./side-sheet";

/** What each copy key is called on the screen. */
export const COPY_LABEL: Record<string, string> = {
  preview_url: "Preview URL", redirect_uri: "Redirect URI", public_url: "BOSS address",
  deploy_url: "Deployed URL",
};
/** A provider's name as people know it ("github" → "GitHub"). */
const PROVIDER_WORD: Record<string, string> = { github: "GitHub", google: "Google", boss: "BOSS" };
export function copyLabel(key: string): string {
  if (COPY_LABEL[key]) return COPY_LABEL[key];
  const cb = /^callback_url:(.+)$/.exec(key);
  if (cb) return `Callback URL for ${PROVIDER_WORD[cb[1]] ?? cb[1]}`;
  const hook = /^webhook_url:(.+)$/.exec(key);
  return hook ? `Webhook URL (${hook[1]})` : key.replace(/_/g, " ");
}
/** A placeholder the API could not fill ("(start a preview to get its URL)"): said, not copied. */
export const unfilled = (v: string): boolean => v.startsWith("(") || v.includes("<preview>");

/** One secret's cell for one environment. */
export function envCell(o: { env: string; testDefault: string; binding: SecretBinding | undefined }) {
  if (o.env === "deploy" && !o.binding) return { word: "Not set", tone: "off" as const, tip: "A deploy gets no value until one is set" };
  const w = secretWord({ testDefault: o.testDefault, binding: o.binding ?? null });
  return { word: w.word, tone: w.tone, tip: w.tip };
}

export type SetupTab = "connectors" | "settings";

export function AppSetupSheet({ projectId, appKey, runId, appName, onClose, tab = "connectors", deployLabel }: {
  projectId: string;
  /** The app's key (the factory row's `app_key`); resolved from `runId` when absent. */
  appKey?: string | null; runId: string; appName: string; onClose: () => void;
  /** The tab it opens on: the row's chip opens Connectors, the build page's Settings opens Settings. */
  tab?: SetupTab;
  /** The app's deploy environment as people call it ("staging"), when it has one. */
  deployLabel?: string;
}) {
  const [key, setKey] = useState<string | null>(appKey ?? null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (appKey) { setKey(appKey); return undefined; }
    let alive = true;
    void resolveAppKey(projectId, runId).then((k) => { if (!alive) return; setKey(k); setMissing(!k); });
    return () => { alive = false; };
  }, [projectId, appKey, runId]);
  return (
    <SideSheet title="Setup" sub={appName} onClose={onClose} testId="tpl-setup-sheet" wide>
      {key ? <AppSetup projectId={projectId} appKey={key} initialTab={tab} deployLabel={deployLabel} />
        : <p className="tpl-muted">{missing ? "This app has no finished build yet." : "Loading…"}</p>}
    </SideSheet>
  );
}

const NO_ADDRESSES: SetupAddresses = { bossPublicUrl: null, preview: { running: false, url: null, reach: null } };
const titleCase = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** The writes every form here makes: one guarded call, one PUT per name (each for every env). */
type UseFor = "preview" | "deploy" | "both";
/** …and the app's own setting: a plain value for deploy (never a live key's approval path). */
type Plan = UseFor | "setting";
const PLAN: Record<Plan, { envs: string[]; mode: "test" | "live" }[]> = {
  setting: [{ envs: ["deploy"], mode: "test" }],
  preview: [{ envs: ["build", "preview"], mode: "test" }],
  deploy: [{ envs: ["deploy"], mode: "live" }],
  both: [{ envs: ["build", "preview"], mode: "test" }, { envs: ["deploy"], mode: "live" }],
};

export function AppSetup({ projectId, appKey, initialTab = "connectors", deployLabel }: {
  projectId: string; appKey: string; initialTab?: SetupTab; deployLabel?: string;
}) {
  const [tab, setTab] = useState<SetupTab>(initialTab);
  const [applied, setApplied] = useState<Applied[] | null>(null);
  const [guides, setGuides] = useState<Guide[]>([]);
  const [addresses, setAddresses] = useState<SetupAddresses>(NO_ADDRESSES);
  const [secrets, setSecrets] = useState<AppSecrets | null>(null);
  const [secretsWhy, setSecretsWhy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const guarded = useGuardedCall();
  const readGate = useGate("app_secret:read_metadata", projectId);
  const ids = useId();
  const dl = deployLabel ? titleCase(deployLabel) : "Deploy";

  const load = useCallback(async () => {
    try {
      const [a, g] = await Promise.all([templatesApi.applied(projectId, appKey), templatesApi.setupGuides(projectId, appKey)]);
      setApplied(a); setGuides(g.guides); setAddresses(g.addresses); setError("");
    } catch (e) {
      setApplied((prev) => prev ?? []);
      setError(refusalWords(e));
    }
    if (!readGate.allowed) { setSecrets(null); setSecretsWhy(readGate.reason); return; }
    try { setSecrets(await templatesApi.secrets(projectId, appKey)); setSecretsWhy(""); }
    catch (e) { setSecrets(null); setSecretsWhy(refusalWords(e)); }
  }, [projectId, appKey, readGate.allowed, readGate.reason]);
  useEffect(() => { void load(); }, [load]);

  const cards = useMemo(() => connectorCards(applied ?? []), [applied]);

  /** Store `values` (name → value) for `use`; one guarded call. Returns whether it stored. */
  async function store(label: string, values: { name: string; value: string }[], use: Plan, done: (decision: boolean) => string): Promise<boolean> {
    if (busy) return false;
    setBusy(true); setError(""); setNote("");
    try {
      let decision = false;
      await guarded(`Store ${label}.`, async () => {
        for (const step of PLAN[use]) {
          for (const v of values) {
            // ONE write for every env the value is for (it was one PUT per env: each key twice).
            const r = await templatesApi.putSecret(projectId, appKey, v.name, { envs: step.envs, mode: step.mode, value: v.value });
            decision = decision || Boolean((r as { live_decision_id?: unknown }).live_decision_id);
          }
        }
      });
      setNote(done(decision));
      void load();
      return true;
    } catch (e) {
      setError(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not stored: you cancelled the check." : refusalWords(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  /** Remove every stored value of `names`, in every env. */
  async function remove(label: string, names: string[]): Promise<void> {
    if (busy) return;
    setBusy(true); setError(""); setNote("");
    try {
      await guarded(`Remove ${label}.`, async () => {
        for (const b of (secrets?.bindings ?? []).filter((x) => names.includes(x.name))) {
          await templatesApi.deleteSecret(projectId, appKey, b.name, b.env);
        }
      });
      setNote(`${label} removed.`);
      void load();
    } catch (e) {
      setError(refusalWords(e));
    } finally {
      setBusy(false);
    }
  }

  const ctx: Ctx = { projectId, appKey, secrets, addresses, guides, busy, store, remove, deployLabel: dl, setNote };
  const tabId = (t: SetupTab) => `${ids}-${t}`;
  return (
    <div className="tpl-setup" data-testid="tpl-setup">
      <div className="tpl-tabs" role="tablist" aria-label="Setup">
        {(["connectors", "settings"] as const).map((t) => (
          <button key={t} type="button" role="tab" id={`${tabId(t)}-tab`} aria-selected={tab === t} aria-controls={`${tabId(t)}-panel`}
                  className="tpl-tab" data-testid={`tpl-tab-${t}`} onClick={() => setTab(t)}
                  title={t === "connectors" ? "Sign-in providers and keys, per connector" : "What BOSS manages, and the settings you set"}>
            {t === "connectors" ? "Connectors" : "Settings"}
          </button>
        ))}
      </div>
      {error ? <p className="tpl-err" role="alert" data-user-text>{error}</p> : null}
      {note ? <p className="tpl-note" role="status" data-testid="tpl-setup-note" data-user-text>{note}</p> : null}
      {secretsWhy ? <p className="tpl-muted" data-testid="tpl-setup-secrets-why">{secretsWhy}</p> : null}

      {tab === "connectors" ? (
        <section className="tpl-sec" role="tabpanel" id={`${tabId("connectors")}-panel`} aria-labelledby={`${tabId("connectors")}-tab`}
                 data-testid="tpl-setup-applied">
          {applied === null ? <span className="tpl-muted">Loading…</span> : null}
          {applied && !cards.length ? (
            <span className="tpl-muted" data-testid="tpl-setup-none">No connector added yet. Add one from the app&apos;s Add a feature › Add a connector.</span>
          ) : null}
          {cards.map((c) => <ConnectorCardView key={c.key} card={c} ctx={ctx} />)}
        </section>
      ) : (
        <section className="tpl-sec" role="tabpanel" id={`${tabId("settings")}-panel`} aria-labelledby={`${tabId("settings")}-tab`}
                 data-testid="tpl-setup-settings">
          {secrets ? <SettingsView ctx={ctx} cards={cards} /> : secretsWhy ? null : <span className="tpl-muted">Loading…</span>}
        </section>
      )}
    </div>
  );
}

type Ctx = {
  projectId: string; appKey: string; secrets: AppSecrets | null; addresses: SetupAddresses; guides: Guide[];
  busy: boolean; deployLabel: string;
  store: (label: string, values: { name: string; value: string }[], use: Plan, done: (decision: boolean) => string) => Promise<boolean>;
  remove: (label: string, names: string[]) => Promise<void>;
  setNote: (s: string) => void;
};

function dateWord(iso: string): string {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : "";
}

// ---- a connector's card ------------------------------------------------------------------------

function ConnectorCardView({ card, ctx }: { card: ConnectorCard; ctx: Ctx }) {
  const bindings = ctx.secrets?.bindings ?? [];
  const declared = ctx.secrets?.declared ?? [];
  const rows = card.providers.map((spec) => providerRow({ spec, bindings, bossPublicUrl: ctx.addresses.bossPublicUrl, deployLabel: ctx.deployLabel }));
  const keys = connectorKeys(card, declared);
  const demo = demoLoginNames(bindings);
  const hasDemo = card.providers.length > 0 && demo.length > 0;
  const where = whereLine({ card, rows, keys, bindings, hasDemo, deployLabel: ctx.deployLabel });
  const guide = ctx.guides.find((g) => g.key === card.key);
  const latest = card.latest;
  const stateWord = APPLIED_WORD[latest.status] ?? latest.status;
  return (
    <article className="tpl-conn" data-testid={`tpl-connector-${card.key}`} aria-labelledby={`tpl-conn-h-${card.key}`}>
      <header className="tpl-conn-head">
        <h3 id={`tpl-conn-h-${card.key}`} className="tpl-conn-name" data-testid={`tpl-applied-${card.key}`}>
          {card.name} <span className="tpl-faint">v{card.version}</span>
          <span className="tpl-chip" data-tone={latest.status === "applied" ? "green" : latest.status === "failed" || latest.status === "blocked" ? "red" : undefined}
                title={latest.blockedReason || (latest.status === "pr_open" ? "Its build opened a PR: merge it to finish" : latest.status === "applied" ? "Merged into the app" : "Its newest build")}>
            {stateWord}
          </span>
        </h3>
        <p className="tpl-conn-sum" data-testid={`tpl-summary-${card.key}`}>{ctx.secrets ? summaryLine({ card, rows, keys, bindings, hasDemo }) : stackLabel(card.stack)}</p>
        {ctx.secrets ? (
          <p className="tpl-where" data-testid={`tpl-where-${card.key}`}>
            <span className="tpl-faint">Where:</span>
            {where.map((w) => (
              <span key={w.env} className="tpl-where-cell" data-tone={w.tone}>{w.env} · {w.word}</span>
            ))}
          </p>
        ) : null}
      </header>

      {ctx.secrets && (rows.length || hasDemo) ? (
        <div className="tpl-prov-table" role="table" aria-label={`${card.name}: sign-in providers`}>
          <div className="tpl-prov-head" role="row">
            <span role="columnheader">Provider</span><span role="columnheader">Status</span><span role="columnheader">What to do</span>
          </div>
          {rows.map((r) => <ProviderRowView key={r.provider} row={r} card={card} guide={guide} ctx={ctx} />)}
          {hasDemo ? (
            <div className="tpl-prov" role="row" data-testid={`tpl-provider-${card.key}-demo`}>
              <span className="tpl-prov-name" role="cell">Demo login</span>
              <span role="cell"><span className="tpl-chip" data-tone="green" title="BOSS generated the demo users' password">Ready</span></span>
              <div className="tpl-prov-do" role="cell">
                <span className="tpl-muted">The app&apos;s demo users sign in with a password BOSS generated ({demo.join(", ")}).</span>
                <DemoLogins projectId={ctx.projectId} appKey={ctx.appKey} compact />
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {ctx.secrets && keys.length ? <KeysTable card={card} keys={keys} ctx={ctx} /> : null}

      <Addresses card={card} guide={guide} ctx={ctx} />

      {guide ? (
        <details className="tpl-fold" data-testid={`tpl-guide-${card.key}`}>
          <summary>Full setup guide</summary>
          <div className="tpl-guide-text md" data-user-text><Markdown text={guide.guide} /></div>
        </details>
      ) : null}

      {card.earlier.length ? (
        <details className="tpl-fold" data-testid={`tpl-connector-history-${card.key}`}>
          <summary>{card.earlier.length} earlier build attempt{card.earlier.length === 1 ? "" : "s"}</summary>
          <ul className="tpl-history">
            {card.earlier.map((a) => (
              <li key={a.id} data-testid={`tpl-attempt-${a.id}`}>
                <span>v{a.version}</span>
                <span className="tpl-faint">{APPLIED_WORD[a.status] ?? a.status}</span>
                {a.createdAt ? <span className="tpl-faint">{dateWord(a.createdAt)}</span> : null}
                {a.blockedReason ? <span className="tpl-faint" data-user-text>{a.blockedReason}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

/** The card's addresses that are not a provider's callback: the preview, the deploy, webhooks. */
function Addresses({ card, guide, ctx }: { card: ConnectorCard; guide: Guide | undefined; ctx: Ctx }) {
  const copy = guide?.copy ?? {};
  const items: { k: string; label: string; value: string }[] = [];
  const pv = ctx.addresses.preview.url;
  if (isUsableAddress(pv)) items.push({ k: "preview_url", label: COPY_LABEL.preview_url, value: pv });
  for (const [k, v] of Object.entries(copy)) {
    if (k === "preview_url" || k === "redirect_uri" || k === "public_url" || k.startsWith("callback_url:")) continue;
    if (k === "deploy_url" && !card.providers.length && unfilled(v)) continue;
    items.push({ k, label: copyLabel(k), value: v });
  }
  const reach = previewReachWords(ctx.addresses);
  if (!items.length && !reach) return null;
  return (
    <div className="tpl-guide-copies" data-testid={`tpl-addresses-${card.key}`}>
      {!isUsableAddress(pv) && reach ? (
        <><span>{COPY_LABEL.preview_url}</span><span className="tpl-faint" data-testid={`tpl-preview-reach-${card.key}`}>{reach}</span></>
      ) : null}
      {items.map(({ k, label, value }) => (
        <React.Fragment key={k}>
          <span>{label}</span>
          {unfilled(value) || !isUsableAddress(value) ? <span className="tpl-faint" data-user-text>{unfilled(value) ? value.replace(/^\(|\)$/g, "") : reach || "Reachable inside BOSS only."}</span>
            : <CopyValue value={value} label={label} testId={`tpl-copy-${card.key}-${k}`} />}
        </React.Fragment>
      ))}
    </div>
  );
}

// ---- a provider's row --------------------------------------------------------------------------

const STEPS: Record<string, (cb: string) => string[]> = {
  google: (cb) => [
    "Open the Google Cloud console › APIs & Services › Credentials › Create credentials › OAuth client ID, type Web application.",
    `Under Authorized redirect URIs, add ${cb}.`,
    "Paste the client ID and client secret below.",
  ],
  github: (cb) => [
    "On GitHub, open Settings › Developer settings › OAuth Apps › New OAuth App.",
    `Set the Authorization callback URL to ${cb}. GitHub takes one callback per app: make one app per address.`,
    "Generate a client secret, then paste the client ID and secret below.",
  ],
};
const stepsFor = (provider: string, cb: string) => (STEPS[provider] ?? ((c: string) => [
  `Register an OAuth client at the provider with the callback ${c}.`, "Paste its client ID and secret below."]))(cb);

function ProviderRowView({ row, card, guide, ctx }: { row: ProviderRow; card: ConnectorCard; guide: Guide | undefined; ctx: Ctx }) {
  return (
    <div className="tpl-prov" role="row" data-testid={`tpl-provider-${card.key}-${row.provider}`} data-status={row.status}>
      <span className="tpl-prov-name" role="cell">{row.name}</span>
      <span role="cell">
        <span className="tpl-chip" data-tone={row.tone} data-testid={`tpl-provider-status-${card.key}-${row.provider}`}>{row.word}</span>
      </span>
      <div className="tpl-prov-do" role="cell">
        {row.provider === "boss" ? <BossDo row={row} ctx={ctx} /> : <OAuthDo row={row} card={card} guide={guide} ctx={ctx} />}
      </div>
    </div>
  );
}

function BossDo({ row, ctx }: { row: ProviderRow; ctx: Ctx }) {
  const state = useBossSignIn(ctx.projectId, ctx.appKey);
  if (row.status === "cant_work" && !state.status?.client) {
    return (
      <div className="tpl-warnbox" role="note" data-testid="tpl-boss-no-address">
        <b>BOSS has no public address yet</b>, so the app cannot send people to it to sign in.
        <span> {PUBLIC_URL_HOWTO}</span>
        <a className="ag-link" href="/identity/sign-in#public-url">Identity › Sign-in › Public URL</a>
      </div>
    );
  }
  return (
    <>
      {ctx.addresses.bossPublicUrl ? (
        <span className="tpl-faint">BOSS is reached at <code>{ctx.addresses.bossPublicUrl}</code>. BOSS registers the app and fills its settings: nothing to paste.</span>
      ) : null}
      <BossSignInDetails state={state} />
    </>
  );
}

function OAuthDo({ row, card, guide, ctx }: { row: ProviderRow; card: ConnectorCard; guide: Guide | undefined; ctx: Ctx }) {
  const { callbacks, missing } = providerCallbacks({ provider: row.provider, guide, addresses: ctx.addresses });
  const [open, setOpen] = useState(row.needsYou);
  const [id, setId] = useState("");
  const [secret, setSecret] = useState("");
  const [use, setUse] = useState<UseFor>("preview");
  const [confirm, setConfirm] = useState(false);
  const fid = useId();
  const key = `${card.key}-${row.provider}`;
  const first = callbacks[0]?.url ?? "the callback address below";
  const tryUrl = isUsableAddress(ctx.addresses.preview.url) ? `${ctx.addresses.preview.url.replace(/\/$/, "")}/auth/login/${row.provider}` : "";
  const set = row.preview !== "none" || row.deploy !== "none";

  async function save() {
    const ok = await ctx.store(`${row.name}'s client ID and secret`,
      [{ name: row.spec.id, value: id.trim() }, { name: row.spec.secret, value: secret.trim() }], use,
      (decision) => [
        `${row.name}'s keys are stored${use === "deploy" ? ` for the ${ctx.deployLabel.toLowerCase()} deploy` : use === "both" ? " for previews and the deploy" : " for previews"}.`,
        decision ? "A deploy uses the live key only after a person approves it in Approvals." : "",
        use !== "deploy" ? (tryUrl ? `Restart the preview, then try it: open ${tryUrl}.` : "Restart the preview, then sign in through the app's own sign-in page to try it.")
          : "The next deploy passes them to the app.",
      ].filter(Boolean).join(" "));
    if (ok) { setId(""); setSecret(""); setOpen(false); }
  }

  return (
    <>
      {callbacks.map((c) => (
        <div key={c.env} className="tpl-cb" data-testid={`tpl-callback-${key}-${c.env}`}>
          <span className="tpl-faint">{c.env === "preview" ? "Preview callback" : `${ctx.deployLabel} callback`}</span>
          <CopyValue value={c.url} label={`${row.name} ${c.env} callback URL`} testId={`tpl-copy-${card.key}-callback_url:${row.provider}-${c.env}`} />
          {c.problem ? <p className="tpl-warnbox" role="note" data-testid={`tpl-warning-${key}-${c.env}`}>{c.problem}</p> : null}
        </div>
      ))}
      {missing.map((m) => <span key={m} className="tpl-faint" data-testid={`tpl-callback-missing-${key}`}>{m}</span>)}
      {open ? (
        <form className="tpl-prov-form" data-testid={`tpl-provider-form-${key}`} onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <ol className="tpl-steps">{stepsFor(row.provider, first).map((s) => <li key={s}>{s}</li>)}</ol>
          <label htmlFor={`${fid}-id`}>Client ID</label>
          <input id={`${fid}-id`} className="tpl-input" autoComplete="off" spellCheck={false} value={id}
                 data-testid={`tpl-provider-id-${key}`} onChange={(e) => setId(e.target.value)} />
          <label htmlFor={`${fid}-secret`}>Client secret</label>
          <input id={`${fid}-secret`} className="tpl-input" type="password" autoComplete="off" spellCheck={false} value={secret}
                 data-testid={`tpl-provider-secret-${key}`} onChange={(e) => setSecret(e.target.value)} />
          <label htmlFor={`${fid}-use`}>Use for</label>
          <select id={`${fid}-use`} className="tpl-input" value={use} data-testid={`tpl-provider-use-${key}`}
                  onChange={(e) => setUse(e.target.value as UseFor)}>
            <option value="preview">Preview (test keys)</option>
            <option value="deploy">{ctx.deployLabel} deploy (live keys, approved before use)</option>
            <option value="both">Both</option>
          </select>
          <span className="tpl-prov-acts">
            <GatedButton permission={use === "preview" ? "app_secret:write" : "app_secret:live"} projectId={ctx.projectId}
                         type="submit" className="cap-btn primary" disabled={ctx.busy || !id.trim() || !secret.trim()}
                         title={id.trim() && secret.trim() ? "Stored write-only: never shown again. BOSS refuses a value that is not the provider's format, where it knows one."
                           : "Paste the client ID and secret first"}
                         data-testid={`tpl-provider-save-${key}`}>{ctx.busy ? "Saving…" : "Save"}</GatedButton>
            {set ? <button type="button" className="cap-btn" onClick={() => setOpen(false)}>Cancel</button> : null}
          </span>
        </form>
      ) : (
        <span className="tpl-prov-acts">
          <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                       data-testid={`tpl-provider-edit-${key}`} disabled={ctx.busy} onClick={() => setOpen(true)}
                       title="Paste new keys (asks you to confirm it is you)">{set ? "Replace keys" : "Set up"}</GatedButton>
          {set ? (confirm ? (
            <>
              <span className="tpl-warn">Remove {row.name}&apos;s keys everywhere?</span>
              <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn danger"
                           data-testid={`tpl-provider-remove-yes-${key}`} disabled={ctx.busy}
                           onClick={() => void ctx.remove(`${row.name}'s keys`, [row.spec.id, row.spec.secret]).then(() => setConfirm(false))}>Remove</GatedButton>
              <button type="button" className="cap-btn" onClick={() => setConfirm(false)}>Keep</button>
            </>
          ) : (
            <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                         data-testid={`tpl-provider-remove-${key}`} disabled={ctx.busy} onClick={() => setConfirm(true)}>Remove</GatedButton>
          )) : null}
        </span>
      )}
    </>
  );
}

// ---- a connector's keys (Stripe's secret key…) and a single secret's form ----------------------

function keyWord(bindings: SecretBinding[], d: DeclaredSecret, env: "preview" | "deploy"): { word: string; tone: string | undefined; tip: string } {
  const c = envCell({ env, testDefault: d.testDefault, binding: bindings.find((b) => b.name === d.name && b.env === env) });
  return { word: c.word, tone: c.tone === "warn" ? "amber" : c.tone === "ok" || c.tone === "live" ? "green" : undefined, tip: c.tip };
}

function KeysTable({ card, keys, ctx }: { card: ConnectorCard; keys: DeclaredSecret[]; ctx: Ctx }) {
  const bindings = ctx.secrets?.bindings ?? [];
  return (
    <div className="tpl-prov-table" role="table" aria-label={`${card.name}: keys`}>
      <div className="tpl-prov-head" role="row">
        <span role="columnheader">Key</span><span role="columnheader">Status</span><span role="columnheader">What to do</span>
      </div>
      {keys.map((d) => {
        const pv = keyWord(bindings, d, "preview");
        const dp = keyWord(bindings, d, "deploy");
        return (
          <div key={d.name} className="tpl-prov" role="row" data-testid={`tpl-setup-secret-${d.name}`}>
            <span className="tpl-prov-name tpl-mono" role="cell">{d.name}</span>
            <span role="cell" className="tpl-envs">
              <span className="tpl-chip" data-tone={pv.tone} title={pv.tip} data-testid={`tpl-env-${d.name}-preview`}>Preview · {pv.word}</span>
              <span className="tpl-chip" data-tone={dp.tone} title={dp.tip} data-testid={`tpl-env-${d.name}-deploy`}>{ctx.deployLabel} · {dp.word}</span>
            </span>
            <div className="tpl-prov-do" role="cell">
              {d.purpose ? <span className="tpl-faint" data-user-text>{d.purpose}</span> : null}
              <SecretActions name={d.name} ctx={ctx} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Set test key (build + preview) · Set live key (deploy, approved before use) · Remove. */
function SecretActions({ name, ctx, removeWord = "Remove" }: { name: string; ctx: Ctx; removeWord?: string }) {
  const [kind, setKind] = useState<"test" | "live" | null>(null);
  const [value, setValue] = useState("");
  const [confirm, setConfirm] = useState(false);
  const mine = (ctx.secrets?.bindings ?? []).filter((b) => b.name === name);
  async function save() {
    if (!kind || !value.trim()) return;
    const ok = await ctx.store(name, [{ name, value: value.trim() }], kind === "live" ? "deploy" : "preview",
      (decision) => (decision ? `${name} stored. A deploy uses it only after a person approves it in Approvals.` : `${name} stored for builds and previews.`));
    if (ok) { setKind(null); setValue(""); }
  }
  return (
    <>
      <span className="tpl-prov-acts">
        <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                     data-testid={`tpl-set-test-${name}`} disabled={ctx.busy}
                     title="Paste a test key for builds and previews (asks you to confirm it is you)"
                     onClick={() => { setKind("test"); setValue(""); ctx.setNote(""); }}>
          {mine.some((b) => b.mode === "test") ? "Replace test key" : "Set test key"}
        </GatedButton>
        <GatedButton permission="app_secret:live" projectId={ctx.projectId} type="button" className="cap-btn"
                     data-testid={`tpl-set-live-${name}`} disabled={ctx.busy}
                     title="A live key, for deploys only: no deploy uses it until a person approves"
                     onClick={() => { setKind("live"); setValue(""); ctx.setNote(""); }}>
          Set live key
        </GatedButton>
        {mine.some((b) => b.mode === "test" || b.mode === "live") ? (confirm ? (
          <>
            <span className="tpl-warn">{removeWord} {name} everywhere?</span>
            <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn danger"
                         data-testid={`tpl-remove-yes-${name}`} disabled={ctx.busy}
                         onClick={() => void ctx.remove(name, [name]).then(() => setConfirm(false))}>{removeWord}</GatedButton>
            <button type="button" className="cap-btn" onClick={() => setConfirm(false)}>Keep</button>
          </>
        ) : (
          <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                       data-testid={`tpl-remove-${name}`} disabled={ctx.busy} title="Remove its stored values"
                       onClick={() => setConfirm(true)}>{removeWord}</GatedButton>
        )) : null}
      </span>
      {kind ? (
        <form className="tpl-row-set" onSubmit={(e) => { e.preventDefault(); void save(); }} data-testid={`tpl-secret-form-${name}`}>
          <input className="tpl-input" type="password" autoComplete="off" spellCheck={false} autoFocus value={value}
                 aria-label={`${kind === "live" ? "Live" : "Test"} key for ${name}`}
                 placeholder={kind === "live" ? "Paste the live key (deploys only)" : "Paste a test key"}
                 data-testid={`tpl-secret-value-${name}`} onChange={(e) => setValue(e.target.value)} />
          <GatedButton permission={kind === "live" ? "app_secret:live" : "app_secret:write"} projectId={ctx.projectId}
                       type="submit" className="cap-btn primary" disabled={ctx.busy || !value.trim()}
                       title={value.trim() ? "Stored write-only: never shown again" : "Paste the value first"}
                       data-testid={`tpl-secret-save-${name}`}>{ctx.busy ? "Saving…" : "Save"}</GatedButton>
          <button type="button" className="cap-btn" onClick={() => { setKind(null); setValue(""); }}>Cancel</button>
        </form>
      ) : null}
    </>
  );
}

// ---- settings ----------------------------------------------------------------------------------

function envsWord(r: SettingRow): string {
  if (!r.envs.length) return "Made when a build or deploy needs it";
  return r.envs.map((e) => ENV_WORD[e.env] ?? e.env).join(", ");
}

function SettingsView({ ctx, cards }: { ctx: Ctx; cards: ConnectorCard[] }) {
  const { managed, yours } = settingsSplit({ secrets: ctx.secrets!, cards });
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);

  async function add() {
    const n = name.trim().toUpperCase();
    if (!DEPLOY_VALUE_NAME.test(n) || !value) return;
    const ok = await ctx.store(`${n} for deploy`, [{ name: n, value }], "setting",
      () => `${n} stored. The next deploy passes it to the app.`);
    if (ok) { setName(""); setValue(""); }
  }
  async function saveEdit(r: SettingRow) {
    if (!editValue) return;
    const ok = await ctx.store(`${r.name} for deploy`, [{ name: r.name, value: editValue }], "setting",
      () => `${r.name} stored. The next deploy passes it to the app.`);
    if (ok) { setEditing(null); setEditValue(""); }
  }

  return (
    <>
      <div className="tpl-set-group" data-testid="tpl-settings-managed">
        <h3 title="Values BOSS made for the app. Read-only: replace one with your own if you must.">Managed by BOSS</h3>
        {managed.length ? (
          <ul className="tpl-rows">
            {managed.map((r) => (
              <li key={r.name} className="tpl-row" data-testid={`tpl-managed-${r.name}`}>
                <span className="tpl-row-main">
                  <span className="tpl-row-name tpl-mono">{r.name}</span>
                  <span className="tpl-faint" data-user-text>{r.how} · {envsWord(r)}{r.purpose ? ` · ${r.purpose}` : ""}</span>
                </span>
                {r.declared ? (
                  <details className="tpl-fold tpl-own">
                    <summary>Use your own value</summary>
                    <SecretActions name={r.name} ctx={ctx} />
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <span className="tpl-muted">BOSS manages nothing for this app yet.</span>}
        {managed.some((r) => r.demo) ? <DemoLogins projectId={ctx.projectId} appKey={ctx.appKey} /> : null}
      </div>

      <div className="tpl-set-group" data-testid="tpl-deploy-values">
        <h3 title="Settings you set: the app reads them at deploy (and preview, when its own files declare them). Write-only.">Your settings</h3>
        {yours.length ? (
          <ul className="tpl-rows">
            {yours.map((r) => (
              <li key={r.name} className="tpl-row" data-testid={`tpl-deploy-value-${r.name}`}>
                <span className="tpl-row-main">
                  <span className="tpl-row-name tpl-mono">{r.name}</span>
                  <span className="tpl-faint">Set by a person · {envsWord(r)}</span>
                </span>
                {/* A connector's secret a person replaced: its test/live keys, as on a card. */}
                {r.declared ? <span className="tpl-row-acts"><SecretActions name={r.name} ctx={ctx} /></span> : (
                <span className="tpl-row-acts">
                  <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                               data-testid={`tpl-deploy-value-edit-${r.name}`} disabled={ctx.busy}
                               title="Give it a new value (the old one is never shown)"
                               onClick={() => { setEditing(r.name); setEditValue(""); }}>Edit</GatedButton>
                  {confirm === r.name ? (
                    <>
                      <span className="tpl-warn">Remove {r.name}?</span>
                      <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn danger"
                                   data-testid={`tpl-deploy-value-remove-yes-${r.name}`} disabled={ctx.busy}
                                   onClick={() => void ctx.remove(r.name, [r.name]).then(() => setConfirm(null))}>Remove</GatedButton>
                      <button type="button" className="cap-btn" onClick={() => setConfirm(null)}>Keep</button>
                    </>
                  ) : (
                    <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="button" className="cap-btn"
                                 data-testid={`tpl-deploy-value-remove-${r.name}`} disabled={ctx.busy}
                                 onClick={() => setConfirm(r.name)}>Remove</GatedButton>
                  )}
                </span>
                )}
                {editing === r.name ? (
                  <form className="tpl-row-set" data-testid={`tpl-deploy-value-edit-form-${r.name}`} onSubmit={(e) => { e.preventDefault(); void saveEdit(r); }}>
                    <input className="tpl-input" type="password" autoComplete="off" autoFocus value={editValue}
                           aria-label={`New value for ${r.name}`} placeholder="New value" onChange={(e) => setEditValue(e.target.value)}
                           data-testid={`tpl-deploy-value-edit-value-${r.name}`} />
                    <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="submit" className="cap-btn primary"
                                 disabled={ctx.busy || !editValue} data-testid={`tpl-deploy-value-edit-save-${r.name}`}>Save</GatedButton>
                    <button type="button" className="cap-btn" onClick={() => setEditing(null)}>Cancel</button>
                  </form>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <span className="tpl-muted">None yet.</span>}
        <form className="tpl-row-set tpl-add-setting" data-testid="tpl-deploy-value-form" onSubmit={(e) => { e.preventDefault(); void add(); }}>
          <input className="tpl-input tpl-mono" value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="NAME, e.g. ALLOW_DEMO_SEED" aria-label="Setting name" spellCheck={false}
                 data-testid="tpl-deploy-value-name" autoComplete="off" />
          <input className="tpl-input" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Value"
                 aria-label="Setting value" type="password" data-testid="tpl-deploy-value-value" autoComplete="off" />
          <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="submit" className="cap-btn primary"
                       disabled={ctx.busy || !DEPLOY_VALUE_NAME.test(name.trim().toUpperCase()) || !value}
                       title="Stored write-only for deploy; the next deploy passes it to the app"
                       data-testid="tpl-deploy-value-save">
            Add setting
          </GatedButton>
        </form>
      </div>
    </>
  );
}

/** DEMO LOGINS: the password the app's demo users sign in with, which BOSS generated for its
 *  previews and its deploy (its seed refuses to run without one). Hidden until asked for, behind
 *  the same check as setting a secret; a value a person set is never shown here. */
export function DemoLogins({ projectId, appKey, compact = false }: { projectId: string; appKey: string; compact?: boolean }) {
  const [shown, setShown] = useState<DemoLogin[] | null>(null);
  const [why, setWhy] = useState("");
  const guarded = useGuardedCall();
  async function show() {
    setWhy("");
    try {
      const all = await guarded("Show the demo login passwords.", async () => [
        ...(await templatesApi.demoLogins(projectId, appKey, "preview")),
        ...(await templatesApi.demoLogins(projectId, appKey, "deploy"))]);
      setShown(all);
    } catch (e) {
      setWhy(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not shown: you cancelled the check." : refusalWords(e));
    }
  }
  return (
    <div className="tpl-demo" data-testid={compact ? "tpl-demo-logins-row" : "tpl-demo-logins"}>
      {compact ? null : <h4 title="The password BOSS generated for the app's demo users, for its previews and its deploy">Demo logins</h4>}
      {shown === null ? (
        <GatedButton permission="app_secret:write" projectId={projectId} type="button" className="cap-btn"
                     data-testid={compact ? "tpl-demo-logins-row-show" : "tpl-demo-logins-show"} onClick={() => void show()}
                     title="Show the demo login passwords BOSS generated (asks you to confirm it is you)">
          {compact ? "Show password" : "Show demo login passwords"}
        </GatedButton>
      ) : shown.length ? (
        <ul className="tpl-rows">
          {shown.map((d) => (
            <li key={`${d.env}-${d.name}`} className="tpl-row" data-testid={`tpl-demo-login-${d.env}-${d.name}`}>
              <span className="tpl-row-name tpl-mono">{d.name}</span>
              <span className="tpl-chip">{ENV_WORD[d.env] ?? d.env}</span>
              <CopyValue value={d.value} label={`${d.name} (${ENV_WORD[d.env] ?? d.env})`} testId={`tpl-demo-login-copy-${d.env}-${d.name}`} />
            </li>
          ))}
        </ul>
      ) : <span className="tpl-muted" data-testid="tpl-demo-logins-none">BOSS has generated no demo login password for this app.</span>}
      {why ? <span className="tpl-err" role="alert" data-user-text>{why}</span> : null}
    </div>
  );
}
