"use client";

/**
 * Add a model: three kinds of thing, never one dropdown mixing them. A subscription you sign
 * into, an API key you paste, or a local model on your hardware. Every Connect asks one tiny
 * question, budgeted and receipted, before saying Working.
 *
 * A subscription (Codex, Claude Code, Grok) is never pasted, dropped or imported: Sign in runs the
 * provider's own login on the BOSS server, then Connect proves it (BOSSAPP-769).
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useRef, useState } from "react";
import type { SignInSession } from "@boss/api-client";

import { SetupFailure, fetchSetup, setupWrite } from "../../lib/setup-client";
import type {
  CatalogueEntry, Detected, HeldProof, ModelEntry, SetupRefusal, SetupView,
} from "../../lib/setup-types";
import { Consent, toast, useBoard } from "./parts";
import { chipStatus, cliSignedIn, providerRow, refusedPill } from "./model-status";
import { Dot, Out, Pill, Refusal, Tip } from "./pill";
import { SIGN_IN_PROVIDERS, SubscriptionSignIn } from "./subscription-sign-in";

/** Where Local › Ollama looks when the URL is left empty, if the server did not say: the host's
 *  Ollama as the Community containers see it (never the container's own 127.0.0.1). */
const OLLAMA_IN_DOCKER = "http://host.docker.internal:11434/v1";
/** A URL as a placeholder short enough for a phone's field: host and port only. */
const urlHint = (url: string): string => {
  try { return url ? new URL(url).host : ""; } catch { return url; }
};

type Tile = "subscription" | "api_key" | "local";

const TILES: Array<{ tile: Tile; label: string; tip: string }> = [
  { tile: "subscription", label: "Subscription", tip: "Your plan. No per-call charge; plan limits apply." },
  { tile: "api_key", label: "API key", tip: "Billed per call to your key. Counts toward the budget." },
  { tile: "local", label: "Local", tip: "Runs on your hardware. Free." },
];

function refusalOf(error: unknown): SetupRefusal {
  if (error instanceof SetupFailure && error.refusal) return error.refusal;
  return { state: "failed", headline: "Check failed", fix: String((error as Error)?.message ?? error) };
}

const usd = (micros: number) => `$${(micros / 1_000_000).toFixed(micros < 10_000 ? 4 : 2)}`;

/**
 * A held proof, in words, with Review → "Release the hold". The hold is money BOSS kept because
 * it could not tell what the provider did; a person who can manage entitlements looks at the
 * receipt's facts and releases it (recorded with their name and reason). Connect waits for it.
 */
export function HeldProofNotice({ hold, onReleased }: {
  hold: HeldProof; onReleased: (next: SetupView) => void;
}) {
  const { projectId } = useBoard();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const release = async () => {
    setBusy(true);
    setRefusal(null);
    try {
      const next = await setupWrite<SetupView>(projectId, "release_hold", {
        receipt_id: hold.receipt_id, reason: reason.trim(),
      }, { label: "Release the hold" });
      toast("Hold released, Connect again", "success");
      onReleased(next);
    } catch (error) {
      setRefusal(refusalOf(error));
    } finally {
      setBusy(false);
    }
  };
  const charge = hold.release.basis === "refused" ? "$0 (the provider refused)"
    : `${usd(hold.release.cost_usd_micros)}${hold.release.basis === "unpriced" ? " (unpriced estimate)" : ""}`;
  return (
    <div className="su-held" role="status" data-testid="su-held-proof">
      <span className="su-held-summary" data-testid="su-held-summary">Proof held: {hold.summary}</span>
      {!open ? (
        <button type="button" className="su-btn" data-testid="su-held-review"
                title="See the receipt and release the hold." onClick={() => setOpen(true)}>
          Review
        </button>
      ) : (
        <div className="su-held-review" data-testid="su-held-facts">
          <dl>
            <dt>Receipt</dt><dd><code>{hold.receipt_id}</code></dd>
            <dt>Model</dt><dd><code>{hold.model}</code></dd>
            {hold.outcome.http_status ? <><dt>Reply</dt><dd>HTTP {hold.outcome.http_status}
              {hold.outcome.provider_error_type ? `, ${hold.outcome.provider_error_type}` : ""}</dd></> : null}
            <dt>Held</dt><dd>{usd(hold.held_usd_micros)}</dd>
            <dt>Release charges</dt><dd data-testid="su-held-charge">{charge}</dd>
          </dl>
          <label className="su-field">
            <span className="su-field-label">Reason</span>
            <input value={reason} maxLength={300} data-testid="su-held-reason" aria-label="Reason"
                   placeholder="Why it is safe to release"
                   onChange={(e) => setReason(e.target.value)} />
          </label>
          <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-primary"
                       disabled={busy || !reason.trim()} data-testid="su-held-release"
                       title="Settle this receipt and free the hold. Recorded with your name and reason."
                       onClick={release}>
            {busy ? "Releasing…" : "Release the hold"}
          </GatedButton>
          {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
        </div>
      )}
    </div>
  );
}

export function AddModelPanel({ initialTile, initialProvider, onDone, replaceSourceId,
                               startSignIn = false }: {
  initialTile: Tile; initialProvider: string; onDone?: () => void;
  /** Replace: the one KEY connection this replaces (a new key never overwrites another). A
   *  subscription is never replaced: it signs in again. */
  replaceSourceId?: string;
  /** Opened from a row's Sign in: start the provider's login at once. */
  startSignIn?: boolean;
}) {
  const { projectId, view, apply } = useBoard();
  const [tile, setTile] = useState<Tile>(initialTile);
  const [provider, setProvider] = useState(initialProvider);
  const [secret, setSecret] = useState("");
  const [session, setSession] = useState<SignInSession | null>(null);
  const [again, setAgain] = useState(startSignIn);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<ModelEntry["pill"] | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const listTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const entry: CatalogueEntry | undefined = view.catalogue.find((c) => c.provider === provider);
  const detected: Detected | undefined = view.detected.find((d) => d.provider === provider);
  const chips = view.catalogue.filter((c) => c.kind === tile);
  // Codex, Claude Code and Grok sign in here (the only subscriptions the catalogue offers).
  const signsIn = tile === "subscription" && SIGN_IN_PROVIDERS.has(provider);
  // WHAT THE ROWS SAY, read the same way (model-status): the provider's Models row is its
  // last real proof; the CLI's own "signed in" only decides that Connect, which proves it and
  // signs nothing out, comes before Sign in. A login the last proof refused says so beside
  // Connect ("Check refused"), and Sign in again (which replaces the login) stays one click
  // away; it is never pressed for the person (2026-10-25: a fresh Claude login was at risk).
  const row = providerRow(view, provider);
  const rowWorks = tile === "subscription" && row?.pill.tone === "green";
  const refusedLogin = view.models.some((m) => m.provider === provider && refusedPill(m.pill));
  const detectedIn = cliSignedIn(view, provider);
  const usable = (detectedIn || rowWorks) && !again;
  const signedIn = session?.state === "signed_in" || usable;
  const notOnServer = detected?.found === "not_on_server";

  useEffect(() => {
    // A new provider is a new form: nothing typed for the last one carries over.
    setSecret(""); setModels([]); setRefusal(null); setResult(null);
    setAgain(startSignIn && provider === initialProvider);
    // REPLACE ASKS FOR THE KEY ONLY: the model and URL of the connection it replaces stay.
    const replacing = view.models.find((m) => m.source_id === replaceSourceId
                                              && m.provider === provider);
    setModel(replacing?.model ?? entry?.example_model ?? "");
    setBaseUrl(replacing?.base_url
               ?? (provider === "ollama" ? (detected?.base_url ?? "") : ""));
    if (provider === "ollama" && detected?.found === "found") void list("", detected.base_url ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  const list = async (key: string, url: string) => {
    try {
      const answer = await setupWrite<{ models: string[] }>(projectId, "model_catalogue", {
        provider, kind: tile, api_key: key || undefined, base_url: url || undefined,
      }, { label: "Model", background: true });
      setModels(answer.models ?? []);
    } catch { setModels([]); }
  };
  const onKey = (value: string) => {
    setSecret(value);
    if (listTimer.current) clearTimeout(listTimer.current);
    if (tile === "api_key" && provider !== "custom" && value.trim().length >= 12) {
      listTimer.current = setTimeout(() => void list(value.trim(), ""), 400);
    }
  };
  useEffect(() => () => { if (listTimer.current) clearTimeout(listTimer.current); }, []);

  // A HELD PROOF BLOCKS CONNECT for its provider until a person reviews and releases it.
  const held = (view.held_proofs ?? []).filter((h) => h.provider === provider);
  // A subscription stores no secret, so it asks no consent to store one.
  const needsConsent = view.secrets.needs_consent && (tile === "api_key" || (tile === "local" && !!secret));
  const fields = entry?.fields ?? [];
  let ready = !busy && held.length === 0 && (!needsConsent || consent);
  if (tile === "subscription") {
    ready = ready && !notOnServer && signedIn;
  } else if (tile === "api_key") {
    // THE PROOF ASKS EXACTLY THE MODEL TYPED: an empty field is not quietly the example.
    // "Other" needs its key too: the backend refuses an API-key connection without one (an
    // unauthenticated local server is Local › Other URL), so Connect never offers that 422.
    ready = ready && !!secret.trim()
            && (provider !== "custom" || !!baseUrl.trim())
            && (!fields.includes("model") || !!model.trim());
  } else {
    ready = ready && !!model.trim() && (provider === "ollama" || !!baseUrl.trim());
  }

  // WHEN CONNECT IS DISABLED, SAY WHY: the button used to keep the same tooltip ("Saves it,
  // then asks one tiny question…") whether or not anything had been typed, so a key pasted but
  // not yet tested, or no key at all, looked identical and silent (boss-1 rc10.9).
  const whyNotReady = (): string => {
    if (busy || ready) return "";
    if (held.length) return "Release the hold first";
    if (needsConsent && !consent) return "Accept to continue first";
    if (tile === "subscription") {
      if (notOnServer) return "Install it on the BOSS server first";
      if (!signsIn) return "Sign in on the BOSS server first";
      if (session && (session.state === "starting" || session.state === "waiting")) return "Finish signing in first";
      if (!signedIn) return "Sign in first";
      return "";
    }
    if (tile === "api_key") {
      if (provider === "custom" && !baseUrl.trim()) return "Add the base URL first";
      if (!secret.trim()) return "Paste a key first";
      if (fields.includes("model") && !model.trim()) return "Name the model first";
      return "";
    }
    if (!model.trim()) return "Name the model first";
    if (provider !== "ollama" && !baseUrl.trim()) return "Add the URL first";
    return "";
  };

  const connect = async () => {
    setBusy(true);
    setRefusal(null);
    setResult(null);
    const sent = secret;
    setSecret("");                                          // cleared on every submit
    try {
      // A SUBSCRIPTION SENDS NO CREDENTIAL: the login already lives on the BOSS server, and
      // Connect only proves it. It replaces nothing (no source_id) and stores no secret.
      const next = await setupWrite<SetupView>(projectId, "models", tile === "subscription"
        ? { provider, kind: tile }
        : {
          provider, kind: tile,
          api_key: sent || undefined,
          base_url: baseUrl || undefined, model: model.trim() || undefined,
          accept_database_fallback: needsConsent ? consent : undefined,
          source_id: replaceSourceId,
        }, { label: "Connect" });
      apply(next);
      // SAY WHAT THE CHECK SAID: a busy provider's key is kept, but it is not "Working".
      const before = new Set(view.models.map((m) => m.source_id));
      const row = next.models.find((m) => m.source_id === replaceSourceId)
        ?? next.models.find((m) => !before.has(m.source_id) && m.provider === provider)
        ?? next.models.find((m) => m.provider === provider);
      const label = row?.pill.label ?? "Working";
      const proven = !row || row.pill.tone === "green";
      toast(`Connected, ${label}`, proven ? "success" : "error");
      // A subscription whose proof failed stays open on its pill, so Sign in again is right here.
      if (tile === "subscription" && row && !proven) {
        setResult(row.pill);
        setSession(null);
        setAgain(true);
        setAttempt((n) => n + 1);                           // a fresh Sign in, not the old one
        return;
      }
      onDone?.();
    } catch (error) {
      const refused = refusalOf(error);
      setRefusal(refused);
      // A held proof shows up on the board at once, with its Review.
      if (refused.receipt_id) void fetchSetup(projectId).then(apply).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  /** The subscription's state, LEFT OF its Connect: signed in by this drawer, signed in on the
   *  server, refused at its last check, or working. Nothing while it still has to sign in. */
  function subscriptionStatus(): React.ReactNode {
    if (session?.state === "signed_in") {
      return (
        <span className="su-pill su-tone-green" role="status" data-testid="su-signin-done"
              title={session.headline} data-tip={session.headline}>
          <Dot tone="green" />Signed in
        </span>
      );
    }
    if (!usable || session) return null;
    if (detectedIn && refusedLogin) {
      const tip = "The CLI says signed in, but the last check was refused. Connect re-checks.";
      return (
        <span className="su-pill su-tone-amber" role="status" data-testid="su-signed-in-on-server"
              data-state="refused" title={tip} data-tip={tip}>
          <Dot tone="amber" />Check refused
        </span>
      );
    }
    if (rowWorks && row) {
      return <span data-testid="su-signed-in-on-server"><Pill pill={row.pill} /></span>;
    }
    const tip = "Signed in on the BOSS server. Connect checks it.";
    return (
      <span className="su-pill su-tone-green" role="status" data-testid="su-signed-in-on-server"
            title={tip} data-tip={tip}>
        <Dot tone="green" />Signed in
      </span>
    );
  }

  return (
    <div className="su-add-model" data-testid="su-add-model">
      <div className="su-tiles" role="tablist" aria-label="Add a model">
        {TILES.map((t) => (
          <button key={t.tile} type="button" role="tab" aria-selected={tile === t.tile}
                  className="su-tile" title={t.tip} data-tip={t.tip}
                  data-testid={`su-tile-${t.tile}`}
                  onClick={() => {
                    setTile(t.tile);
                    const firstChip = view.catalogue.find((c) => c.kind === t.tile);
                    if (firstChip) setProvider(firstChip.provider);
                  }}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="su-chips" role="radiogroup" aria-label="Provider">
        {chips.map((c) => {
          // THE ROW'S OWN STATUS when it has a row, else what the server found, the same
          // answer the list shows (a "Sign in" chip sat beside a "Working" row).
          const st = chipStatus(view, c.provider);
          const show = !!st && (!!st.pill || tile === "subscription" || st.found === "found");
          return (
            <button key={c.provider} type="button" role="radio" aria-checked={provider === c.provider}
                    className="su-provider" data-testid={`su-provider-${c.provider}`}
                    onClick={() => setProvider(c.provider)}>
              {c.label}
              {show && st!.pill ? (
                <span className="su-found su-found-pill" data-tip={st!.tip} title={st!.tip}
                      data-state={st!.pill.state} data-testid="su-provider-status">
                  <Dot tone={st!.pill.tone} />{st!.label}
                </span>
              ) : show ? (
                <Tip tip={st!.tip} className={`su-found su-found-${st!.found}`} testid="su-provider-status">{st!.label}</Tip>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="su-form">
        {tile === "subscription" && notOnServer ? (
          <Tip tip="Install it on the BOSS server, then sign in here." className="su-faint"
               testid="su-not-on-server">Not on server</Tip>
        ) : null}
        {tile === "subscription" && !signsIn && !notOnServer && !detectedIn ? (
          <Tip tip="Sign in on the BOSS server, then Connect." className="su-faint">Not signed in</Tip>
        ) : null}
        {signsIn && !notOnServer && (!usable || session) && session?.state !== "signed_in" ? (
          <SubscriptionSignIn key={`${provider}:${attempt}`} provider={provider}
                              label={entry?.label ?? provider}
                              autoStart={startSignIn && attempt === 0}
                              fresh={startSignIn || again || refusedLogin} onSession={setSession} />
        ) : null}
        {result ? <Pill pill={result} testid="su-connect-result" /> : null}

        {fields.includes("base_url") ? (
          <label className="su-field">
            <span className="su-field-label">{tile === "local" ? "URL" : "Base URL"}</span>
            <input type="url" value={baseUrl} data-testid="su-model-base-url"
                   aria-label={tile === "local" ? "URL" : "Base URL"}
                   placeholder={urlHint(entry?.example_base_url
                                    || (provider === "ollama" ? OLLAMA_IN_DOCKER : ""))
                                || "http://host:8000/v1"}
                   onChange={(e) => setBaseUrl(e.target.value)}
                   onBlur={() => { if (tile === "local" && baseUrl) void list("", baseUrl); }} />
          </label>
        ) : null}
        {tile !== "subscription" && fields.includes("api_key") ? (
          <label className="su-field">
            <span className="su-field-label">API key</span>
            <input type="password" autoComplete="off" spellCheck={false} value={secret}
                   data-testid="su-model-key" aria-label="API key"
                   title="Stored in BOSS's secret store. Never shown again."
                   onChange={(e) => onKey(e.target.value)} />
          </label>
        ) : null}
        {tile !== "subscription" && fields.includes("model") ? (
          <label className="su-field">
            <span className="su-field-label">Model</span>
            <input list="su-model-options" value={model} data-testid="su-model-name"
                   aria-label="Model" onChange={(e) => setModel(e.target.value)} />
            <datalist id="su-model-options">
              {models.map((m) => <option key={m} value={m} />)}
            </datalist>
          </label>
        ) : null}
        {entry?.key_url ? <Out href={entry.key_url}>Get a key</Out> : null}
        {needsConsent ? <Consent checked={consent} onChange={setConsent} /> : null}
        <div className="su-line su-connect-bar">
          {tile === "subscription" && !notOnServer ? subscriptionStatus() : null}
          {/* ONE STATE, ONE ACTION: a subscription already Working is not offered Connect again
              (its row's ⋯ › Test re-checks it); Sign in again stays one click away. */}
          {tile === "subscription" && rowWorks && !session && !again ? null : (
          <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-primary" disabled={!ready}
                  data-testid="su-model-connect"
                  title={whyNotReady() || "Saves it, then asks one tiny question. Budgeted and receipted."}
                  data-tip={whyNotReady() || "Saves it, then asks one tiny question. Budgeted and receipted."}
                  onClick={connect}>
            {busy ? "Checking…" : "Connect"}
          </GatedButton>
          )}
          {tile === "subscription" && signsIn && usable && !session ? (
            <button type="button" className="su-link" data-testid="su-signin-again"
                    title="Signs the current login out, then runs the provider's sign-in."
                    data-tip="Signs the current login out, then runs the provider's sign-in."
                    onClick={() => setAgain(true)}>Sign in again</button>
          ) : null}
          {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
        </div>
        {refusal?.summary && !held.length ? (
          <span className="su-faint" data-testid="su-refusal-summary">{refusal.summary}</span>
        ) : null}
        {held.map((h) => (
          <HeldProofNotice key={h.receipt_id} hold={h}
                           onReleased={(next) => { apply(next); setRefusal(null); }} />
        ))}
      </div>
    </div>
  );
}
