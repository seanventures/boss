"use client";

import React, { useCallback, useEffect, useState } from "react";
import type { HardwarePlace } from "@boss/api-client";

import {
  DEFAULT_PLACE_DETAIL,
  DEFAULT_PLACE_LINE,
  DEFAULT_PLACE_TITLE,
  EMPTY_EXTRAS_COPY,
  SAME_AGENT_COPY,
  agentStateCopy,
  enrollCommand,
  enrollHardwareAgent,
  explainRawSecret,
  fetchHardwarePlaces,
  isLocalPlace,
  registerHardwareProvider,
  registerLocalPlace,
  resolveHardwarePlace,
  selectedPlace,
} from "../lib/hardware";

const PROVIDERS = [
  { value: "aws", label: "AWS" },
  { value: "digitalocean", label: "DigitalOcean" },
  { value: "generic", label: "Other" },
] as const;

function placeFacts(place: HardwarePlace): string {
  return [place.hostname, place.arch, place.os].filter(Boolean).join(" · ");
}

export function HardwareManagerLink() {
  return (
    <a className="cap-btn" href="#hardware-manager" data-testid="hardware-manager-link">
      Where apps install
    </a>
  );
}

export function HardwareManager({ projectId, actor }: { projectId: string; actor: string }) {
  const [places, setPlaces] = useState<HardwarePlace[]>([]);
  const [defaultPlaceId, setDefaultPlaceId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [extra, setExtra] = useState<"agent" | "provider" | null>(null);
  const [providerKey, setProviderKey] = useState<string>("aws");
  const [agentCommand, setAgentCommand] = useState<string | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [providerNote, setProviderNote] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoadError(null);
    const [listed, resolved] = await Promise.all([
      fetchHardwarePlaces(projectId),
      resolveHardwarePlace(projectId),
    ]);
    const next = [...listed.places];
    if (resolved && !next.some((place) => place.id === resolved.id)) {
      next.unshift(resolved);
    }
    setPlaces(next);
    setDefaultPlaceId(listed.default_place_id ?? resolved?.id ?? null);
  }, [projectId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    reload()
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : "Could not load places.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const current = selectedPlace(places, defaultPlaceId);
  const extras = places.filter((place) => place.id !== current?.id);
  const localAgent = agentStateCopy(current?.agent_state);

  async function onLocal(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setLocalError(null);
    try {
      await registerLocalPlace({
        project_id: projectId,
        actor,
        hostname: String(form.get("hostname") ?? "").trim(),
        arch: String(form.get("arch") ?? "").trim(),
        os: String(form.get("os") ?? "").trim(),
      });
      await reload();
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "Could not record this computer.");
    } finally {
      setBusy(false);
    }
  }

  async function onAgent(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("name") ?? "").trim();
    if (!name) {
      setAgentError("Name the machine you already have.");
      return;
    }
    setBusy(true);
    setAgentError(null);
    setAgentCommand(null);
    try {
      const body = await enrollHardwareAgent({ project_id: projectId, actor, name });
      const command = enrollCommand(body);
      if (!command) {
        setAgentError("The enroll command did not come back. Nothing was run.");
        return;
      }
      setAgentCommand(command);
      await reload();
    } catch (error) {
      setAgentError(error instanceof Error ? error.message : "Could not prepare the enroll command.");
    } finally {
      setBusy(false);
    }
  }

  async function onProvider(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const provider = String(form.get("provider") ?? providerKey).trim();
    const secret_ref = String(form.get("secret_ref") ?? "").trim();
    const raw = explainRawSecret(secret_ref);
    if (!name || !provider) {
      setProviderError("Name and provider are required.");
      setProviderNote(null);
      return;
    }
    if (raw) {
      setProviderError(raw);
      setProviderNote(null);
      return;
    }
    const body = { project_id: projectId, actor, name, provider, secret_ref };
    setBusy(true);
    setProviderError(null);
    setProviderNote(null);
    try {
      await registerHardwareProvider(body);
      setProviderNote("Account registered. Nothing was created or changed on the provider.");
      await reload();
    } catch (error) {
      setProviderError(error instanceof Error ? error.message : "Could not register the account.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="hw-manager" data-testid="hardware-manager">
      <header className="section-head">
        <div>
          <span className="eyebrow">Where apps install</span>
          <h2>This computer is where BOSS was installed.</h2>
        </div>
      </header>

      {loading ? <p className="detail">Loading places…</p> : null}
      {loadError ? <p className="detail" role="alert">{loadError}</p> : null}

      {!loading && !loadError ? (
        <>
          <article
            className="ct-block hw-place"
            data-testid="hardware-default-place"
            data-selected="true"
            data-kind={current?.kind ?? "local"}
            aria-current="true"
          >
            <div className="ct-block-title">
              {DEFAULT_PLACE_TITLE}
              <span className="cap-badge tone-green">Selected</span>
            </div>
            <p className="detail" style={{ margin: 0 }}>{DEFAULT_PLACE_LINE}</p>
            <p className="detail" style={{ margin: 0 }}>{DEFAULT_PLACE_DETAIL}</p>
            <p
              className="detail"
              data-testid="hardware-local-agent"
              data-agent-state={localAgent.state}
              style={{ margin: 0 }}
            >
              {localAgent.label}
            </p>
            {current ? (
              <p className="detail" style={{ margin: 0 }}>
                <strong>{current.name}</strong>
                {placeFacts(current) ? <> · {placeFacts(current)}</> : null}
              </p>
            ) : (
              <p className="detail" style={{ margin: 0 }}>
                The installer record is not in the list yet. Save this computer below — apps still belong here.
              </p>
            )}
          </article>

          <details className="dm-advanced">
            <summary className="detail">Correct this computer&apos;s record</summary>
            <form className="cap-add hw-form" onSubmit={onLocal} style={{ marginTop: 10 }}>
              <div className="cap-add-row">
                <label>
                  Hostname
                  <input name="hostname" required defaultValue={current?.hostname ?? ""} autoComplete="off" />
                </label>
                <label>
                  Architecture
                  <input name="arch" required defaultValue={current?.arch ?? ""} placeholder="arm64" autoComplete="off" />
                </label>
                <label>
                  Operating system
                  <input name="os" required defaultValue={current?.os ?? ""} placeholder="linux" autoComplete="off" />
                </label>
              </div>
              {localError ? <p className="detail" role="alert">{localError}</p> : null}
              <div>
                <button className="cap-btn" type="submit" disabled={busy}>Save this computer</button>
              </div>
            </form>
          </details>

          <section className="ct-block" aria-label="Additional hardware" data-testid="hardware-extras">
            <div className="ct-block-title">Additional hardware</div>
            <p className="detail" style={{ margin: 0 }}>
              Additional hardware is optional. This computer is already where apps install.
              Any machine you add gets the same hardware agent.
            </p>
            {extras.length === 0 ? (
              <p className="detail" data-testid="hardware-extra-empty" style={{ margin: 0 }}>
                {EMPTY_EXTRAS_COPY}
              </p>
            ) : (
              <ul className="hw-extra-list">
                {extras.map((place) => (
                  <li key={place.id} data-testid="hardware-extra-place" data-selected="false" aria-current="false">
                    <strong>{place.name}</strong>
                    <span className="detail">
                      {" "}
                      {isLocalPlace(place) ? "Computer" : place.kind === "provider" ? "Provider" : "Machine"}
                      {place.provider ? ` · ${place.provider}` : ""}
                      {placeFacts(place) ? ` · ${placeFacts(place)}` : ""}
                      {" · "}
                      {agentStateCopy(place.agent_state).state === "installed"
                        ? "Hardware agent installed"
                        : "Hardware agent not ready"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <div className="hw-extra-actions">
              <button
                type="button"
                className="cap-btn"
                aria-expanded={extra === "agent"}
                onClick={() => setExtra(extra === "agent" ? null : "agent")}
              >
                Add hardware
              </button>
              <button
                type="button"
                className="cap-btn"
                aria-expanded={extra === "provider"}
                onClick={() => setExtra(extra === "provider" ? null : "provider")}
              >
                Connect a provider
              </button>
            </div>

            {extra === "agent" ? (
              <form className="cap-add hw-form" data-testid="hardware-agent-panel" onSubmit={onAgent}>
                <p className="detail" style={{ margin: 0 }}>
                  Add hardware installs this same agent on a VM or bare-metal server you already have.
                  Run one command there. The machine installs its dependencies and calls home.
                  {" "}{SAME_AGENT_COPY}
                </p>
                <div className="cap-add-row">
                  <label>
                    Machine name
                    <input name="name" required placeholder="build-vm" autoComplete="off" />
                  </label>
                </div>
                {agentError ? <p className="detail" role="alert">{agentError}</p> : null}
                <div>
                  <button className="cap-btn primary" type="submit" disabled={busy}>
                    Show enroll command
                  </button>
                </div>
                {agentCommand ? (
                  <>
                    <p className="detail" style={{ margin: 0 }}>
                      Run this on that server. It installs the same hardware agent and calls home.
                    </p>
                    <pre className="hw-command" data-testid="hardware-enroll-command">{agentCommand}</pre>
                  </>
                ) : null}
              </form>
            ) : null}

            {extra === "provider" ? (
              <form className="cap-add hw-form" data-testid="hardware-provider-panel" onSubmit={onProvider}>
                <p className="detail" data-testid="hardware-provider-proposal" style={{ margin: 0 }}>
                  Register the account. This does not create servers, change DNS, or spend money.
                  {" "}{SAME_AGENT_COPY}
                </p>
                <div className="cap-add-row">
                  <label>
                    Name
                    <input name="name" required placeholder="Production" autoComplete="off" />
                  </label>
                  <label>
                    Provider
                    <select
                      name="provider"
                      value={providerKey}
                      onChange={(event) => setProviderKey(event.target.value)}
                    >
                      {PROVIDERS.map((provider) => (
                        <option key={provider.value} value={provider.value}>{provider.label}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Secret reference
                    <input
                      name="secret_ref"
                      required
                      placeholder="env:BOSS_AWS_SECRET"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </label>
                </div>
                <p className="detail" style={{ margin: 0 }}>
                  A name from the secret store. Do not paste the secret.
                </p>
                {providerError ? <p className="detail" role="alert">{providerError}</p> : null}
                {providerNote ? <p className="detail" data-testid="hardware-provider-note">{providerNote}</p> : null}
                <div>
                  <button className="cap-btn primary" type="submit" disabled={busy}>Register account</button>
                </div>
              </form>
            ) : null}
          </section>
        </>
      ) : null}
    </div>
  );
}
