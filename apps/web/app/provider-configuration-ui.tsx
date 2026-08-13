"use client";

import {
  ArrowSquareOut,
  CaretRight,
  ChartBar,
  CheckCircle,
  Cloud,
  Database,
  Gauge,
  Key,
  Lock,
  MagnifyingGlass,
  PlugsConnected,
  ShieldCheck,
  Warning,
  X,
} from "@phosphor-icons/react";
import { useEffect, useMemo, useState } from "react";

import { formatDateTime as formatDateTimeUtc } from "../lib/datetime";
import type { ActivityFeedItem } from "../lib/events";
import type {
  CostEventFeedItem,
  ExternalResourceFeedItem,
  ProviderAccountFeedItem,
  ProviderDefinitionFeedItem,
  ProviderOpsFeed,
} from "../lib/providerops";
import {
  disableProviderAccountAction,
  enableProviderAccountAction,
  revokeProviderAccountAction,
  rotateProviderCredentialsAction,
  updateProviderAccountAction,
} from "./actions";
import {
  ActionForm,
  ProviderCreateForm,
  ProviderSyncForm,
  ProviderTestForm,
} from "./client-forms";

type ProviderView = "connections" | "resources" | "health" | "finops";

type SavedScope = {
  provider: string;
  account: string;
  environment: string;
  region: string;
  service: string;
};

const EMPTY_SCOPE: SavedScope = {
  provider: "",
  account: "",
  environment: "",
  region: "",
  service: "",
};

const VIEWS: Array<{
  id: ProviderView;
  label: string;
  detail: string;
  icon: typeof PlugsConnected;
}> = [
  {
    id: "connections",
    label: "Connections",
    detail: "Providers and credentials",
    icon: PlugsConnected,
  },
  {
    id: "resources",
    label: "Resources",
    detail: "Inventory and ownership",
    icon: Database,
  },
  {
    id: "health",
    label: "Scaling & Health",
    detail: "Capacity and service posture",
    icon: Gauge,
  },
  {
    id: "finops",
    label: "Cloud FinOps",
    detail: "Spend and reconciliation",
    icon: ChartBar,
  },
];

function titleCase(value: string): string {
  return value
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatDate(value?: string): string {
  // UTC absolute — matches SSR and every browser (React #418).
  return formatDateTimeUtc(value, "Never");
}

function formatMicros(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value / 1_000_000);
}

function statusTone(status: string): "good" | "warn" | "danger" | "muted" {
  const normalized = status.toLowerCase();
  if (
    ["active", "healthy", "available", "succeeded", "applied", "completed"].includes(
      normalized,
    )
  ) {
    return "good";
  }
  if (
    ["revoked", "failed", "error", "unavailable", "blocked"].includes(normalized)
  ) {
    return "danger";
  }
  if (
    ["disabled", "planned", "pending", "stale", "degraded", "unknown"].includes(
      normalized,
    )
  ) {
    return "warn";
  }
  return "muted";
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`provider-status ${statusTone(status)}`}>
      <span aria-hidden />
      {titleCase(status)}
    </span>
  );
}

export function accountFreshness(
  account: ProviderAccountFeedItem,
  refreshedAt: string,
): "fresh" | "stale" | "untested" {
  if (!account.lastTestedAt) {
    return "untested";
  }
  const age =
    new Date(refreshedAt).getTime() - new Date(account.lastTestedAt).getTime();
  return age > 24 * 60 * 60 * 1000 ? "stale" : "fresh";
}

function metadataToken(
  resource: ExternalResourceFeedItem,
  key: "region" | "service",
): string {
  const direct = resource.metadata[key];
  if (typeof direct === "string" && direct.trim()) {
    return direct.trim();
  }
  const prefix = `${key}:`;
  const tag = resource.tags.find((item) => item.toLowerCase().startsWith(prefix));
  return tag ? tag.slice(prefix.length) : "";
}

export function matchesScope(
  resource: ExternalResourceFeedItem,
  scope: SavedScope,
): boolean {
  return (
    (!scope.provider || resource.provider === scope.provider) &&
    (!scope.account || resource.providerAccountId === scope.account) &&
    (!scope.environment || resource.environment === scope.environment) &&
    (!scope.region || metadataToken(resource, "region") === scope.region) &&
    (!scope.service || metadataToken(resource, "service") === scope.service)
  );
}

function definitionForAccount(
  definitions: ProviderDefinitionFeedItem[],
  account: ProviderAccountFeedItem,
): ProviderDefinitionFeedItem | undefined {
  return definitions.find((definition) => definition.provider === account.provider);
}

function ProviderCatalog({
  definitions,
  selectedProvider,
  onSelect,
}: {
  definitions: ProviderDefinitionFeedItem[];
  selectedProvider: string;
  onSelect: (provider: string) => void;
}) {
  if (definitions.length === 0) {
    return (
      <div className="provider-empty compact">
        <Cloud size={22} aria-hidden />
        <div>
          <strong>Provider catalog unavailable</strong>
          <p>The connection API is reachable, but no provider definitions loaded.</p>
        </div>
      </div>
    );
  }
  return (
    <div className="provider-catalog" aria-label="Infrastructure provider catalog">
      {definitions.map((definition) => (
        <button
          className={definition.provider === selectedProvider ? "active" : ""}
          key={definition.provider}
          type="button"
          onClick={() => onSelect(definition.provider)}
          aria-pressed={definition.provider === selectedProvider}
        >
          <span className="provider-catalog-mark" aria-hidden>
            {definition.displayName.slice(0, 2).toUpperCase()}
          </span>
          <span>
            <strong>{definition.displayName}</strong>
            <small>
              {definition.capabilities.length} declared capabilities ·{" "}
              {definition.category}
            </small>
          </span>
          <StatusBadge status={definition.adapterStatus} />
        </button>
      ))}
    </div>
  );
}

function ConnectionCreate({
  projectId,
  actor,
  canManage,
  definition,
}: {
  projectId: string;
  actor: string;
  canManage: boolean;
  definition?: ProviderDefinitionFeedItem;
}) {
  const [authMethod, setAuthMethod] = useState(
    definition?.defaultAuthMethod ?? "",
  );

  useEffect(() => {
    setAuthMethod(definition?.defaultAuthMethod ?? "");
  }, [definition]);

  if (!definition) {
    return null;
  }
  const selectedAuth =
    definition.authMethods.find((method) => method.key === authMethod) ??
    definition.authMethods[0];
  const available =
    definition.adapterStatus === "available" &&
    selectedAuth?.runtimeStatus === "available";
  const blocked = !canManage || !available;

  return (
    <ProviderCreateForm className="provider-create-form">
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="actor" value={actor} />
      <input type="hidden" name="provider" value={definition.provider} />
      <div className="provider-form-heading">
        <div>
          <span className="eyebrow">New connection</span>
          <h3>{definition.displayName}</h3>
        </div>
        <StatusBadge status={available ? "available" : "planned"} />
      </div>
      {!available ? (
        <div className="provider-notice warn" role="note">
          <Warning size={17} aria-hidden />
          This adapter is planned. BOSS will not accept credentials or imply that
          the provider is operational.
        </div>
      ) : null}
      <div className="provider-form-grid">
        <label className="field">
          <span>Account label</span>
          <input
            className="input"
            name="account_label"
            placeholder={`${definition.displayName} production`}
            required
            disabled={blocked}
          />
        </label>
        <label className="field">
          <span>Authentication method</span>
          <select
            className="input"
            name="auth_method"
            value={authMethod}
            onChange={(event) => setAuthMethod(event.target.value)}
            disabled={blocked}
          >
            {definition.authMethods.map((method) => (
              <option
                key={method.key}
                value={method.key}
                disabled={method.runtimeStatus !== "available"}
              >
                {titleCase(method.key)} · {method.runtimeStatus}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="provider-secret-grid">
        {(selectedAuth?.credentialSlots ?? []).map((slot) => (
          <label className="field" key={slot.key}>
            <span>
              {titleCase(slot.key)}
              {slot.required ? " · required" : " · optional"}
            </span>
            <input
              className="input"
              name={`credential_ref__${slot.key}`}
              type="password"
              autoComplete="new-password"
              placeholder="SecretRef pointer"
              required={slot.required}
              disabled={blocked}
            />
            <small>
              Write-only {titleCase(slot.secretKind)} reference. Secret material
              never enters BOSS.
            </small>
          </label>
        ))}
      </div>
      <label className="field">
        <span>Granted provider scopes</span>
        <textarea
          className="textarea"
          name="scopes"
          rows={2}
          placeholder={"One scope per line\nread\nbilling:read"}
          disabled={blocked}
        />
      </label>
      <div className="provider-toggle-grid">
        {[
          ["read_only", "Read-only", true],
          ["billing_readable", "Billing readable", false],
          ["deploy_capable", "Deploy capable", false],
          [
            "destructive_actions_disabled",
            "Destructive actions disabled",
            true,
          ],
        ].map(([name, label, checked]) => (
          <label className="provider-toggle" key={String(name)}>
            <input
              type="checkbox"
              name={String(name)}
              value="true"
              defaultChecked={Boolean(checked)}
              disabled={blocked}
            />
            <span>{String(label)}</span>
          </label>
        ))}
      </div>
      <button className="button primary" type="submit" disabled={blocked}>
        <Lock size={15} aria-hidden />
        Save connection
      </button>
    </ProviderCreateForm>
  );
}

function ProviderAccountTable({
  feed,
  canManage,
  onSelect,
}: {
  feed: ProviderOpsFeed;
  canManage: boolean;
  onSelect: (account: ProviderAccountFeedItem) => void;
}) {
  if (feed.providerAccounts.length === 0) {
    return (
      <div className="provider-empty">
        <PlugsConnected size={28} aria-hidden />
        <div>
          <strong>No provider connections</strong>
          <p>
            Choose an available provider above to register the first project-scoped
            connection.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="provider-table-wrap">
      <table className="provider-table">
        <caption className="sr-only">Configured infrastructure providers</caption>
        <thead>
          <tr>
            <th scope="col">Connection</th>
            <th scope="col">State</th>
            <th scope="col">Credential posture</th>
            <th scope="col">Last test</th>
            <th scope="col">Authority</th>
            <th scope="col">
              <span className="sr-only">Open details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {feed.providerAccounts.map((account) => {
            const freshness = accountFreshness(account, feed.refreshedAt);
            return (
              <tr key={account.id}>
                <td>
                  <strong>{account.accountLabel}</strong>
                  <small>
                    {titleCase(account.provider)} · project scope
                  </small>
                </td>
                <td>
                  <StatusBadge status={account.status} />
                  {account.lastError ? (
                    <small className="provider-error-text">Recorded error</small>
                  ) : null}
                </td>
                <td>
                  <span className="provider-credential-state">
                    <Key size={14} aria-hidden />
                    {Object.keys(account.credentialRefs).length} named slot
                    {Object.keys(account.credentialRefs).length === 1 ? "" : "s"}
                  </span>
                  <small>Version {account.credentialVersion}</small>
                </td>
                <td>
                  <StatusBadge status={freshness} />
                  <small>{formatDate(account.lastTestedAt)}</small>
                </td>
                <td>
                  <strong>
                    {account.readOnly
                      ? "Read only"
                      : account.deployCapable
                        ? "Deploy capable"
                        : "Custom"}
                  </strong>
                  <small>
                    {account.destructiveActionsDisabled
                      ? "Destructive blocked"
                      : "Destructive allowed"}
                  </small>
                </td>
                <td>
                  <button
                    className="provider-row-action"
                    type="button"
                    onClick={() => onSelect(account)}
                    aria-label={`Open ${account.accountLabel} connection details`}
                  >
                    {canManage ? "Manage" : "Inspect"}
                    <CaretRight size={14} aria-hidden />
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AccountDrawer({
  account,
  definition,
  feed,
  events,
  projectId,
  actor,
  canManage,
  onClose,
}: {
  account: ProviderAccountFeedItem;
  definition?: ProviderDefinitionFeedItem;
  feed: ProviderOpsFeed;
  events: ActivityFeedItem[];
  projectId: string;
  actor: string;
  canManage: boolean;
  onClose: () => void;
}) {
  const accountResources = feed.resources.filter(
    (resource) => resource.providerAccountId === account.id,
  );
  const accountCosts = feed.costs.filter(
    (cost) => cost.providerAccountId === account.id,
  );
  const accountReceipts = feed.resourceReceipts.filter(
    (receipt) => receipt.providerAccountId === account.id,
  );
  const accountChanges = feed.changeSets.filter(
    (changeSet) => changeSet.providerAccountId === account.id,
  );
  const accountEvents = events.filter((event) => event.entityId === account.id);
  const slots =
    definition?.authMethods.find((method) => method.key === account.authMethod)
      ?.credentialSlots ?? [];
  const available = definition?.adapterStatus === "available";
  const canOperate =
    canManage && available && account.status !== "revoked";
  const observedSpend = accountCosts.reduce(
    (total, cost) => total + cost.amountUsdMicros,
    0,
  );
  const lastError =
    typeof account.lastError?.message === "string"
      ? account.lastError.message
      : typeof account.lastError?.detail === "string"
        ? account.lastError.detail
        : account.lastError
          ? "A redacted provider error is recorded."
          : undefined;

  return (
    <>
      <button
        type="button"
        className="provider-drawer-scrim"
        onClick={onClose}
        aria-label="Close provider details"
      />
      <aside
        className="provider-drawer"
        role="dialog"
        aria-modal="false"
        aria-labelledby="provider-drawer-title"
      >
        <header>
          <div>
            <span className="eyebrow">{titleCase(account.provider)}</span>
            <h2 id="provider-drawer-title">{account.accountLabel}</h2>
            <p>
              Project-scoped · owned by {account.ownerActor ?? account.createdBy}
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close details">
            <X size={19} aria-hidden />
          </button>
        </header>

        <div className="provider-drawer-status">
          <StatusBadge status={account.status} />
          <StatusBadge status={accountFreshness(account, feed.refreshedAt)} />
          <span>Credential v{account.credentialVersion}</span>
        </div>

        {lastError ? (
          <div className="provider-notice danger" role="alert">
            <Warning size={17} aria-hidden />
            <span>{lastError}</span>
          </div>
        ) : null}

        <section aria-labelledby="provider-impact-title">
          <div className="provider-drawer-section-head">
            <div>
              <span className="eyebrow">Current impact</span>
              <h3 id="provider-impact-title">Resource and spend posture</h3>
            </div>
          </div>
          <dl className="provider-impact-grid">
            <div>
              <dt>Resources</dt>
              <dd>{accountResources.length}</dd>
            </div>
            <div>
              <dt>Observed spend</dt>
              <dd>{formatMicros(observedSpend)}</dd>
            </div>
            <div>
              <dt>Open ChangeSets</dt>
              <dd>
                {
                  accountChanges.filter((item) => item.status !== "applied")
                    .length
                }
              </dd>
            </div>
            <div>
              <dt>Receipts</dt>
              <dd>{accountReceipts.length}</dd>
            </div>
          </dl>
        </section>

        <section aria-labelledby="provider-credentials-title">
          <div className="provider-drawer-section-head">
            <div>
              <span className="eyebrow">Credentials</span>
              <h3 id="provider-credentials-title">Write-only SecretRefs</h3>
            </div>
          </div>
          <div className="provider-secret-list">
            {Object.keys(account.credentialRefs).map((slot) => (
              <div key={slot}>
                <span>
                  <Key size={14} aria-hidden />
                  {titleCase(slot)}
                </span>
                <code>•••••• configured</code>
              </div>
            ))}
            {Object.keys(account.credentialRefs).length === 0 ? (
              <p>No named credential references are configured.</p>
            ) : null}
          </div>
          <p className="provider-helper">
            Reference values and secret material are never rendered in this view.
            Rotations create bounded predecessor overlap and clear stale test
            evidence.
          </p>
        </section>

        {canManage ? (
          <section aria-labelledby="provider-controls-title">
            <div className="provider-drawer-section-head">
              <div>
                <span className="eyebrow">Controls</span>
                <h3 id="provider-controls-title">Connection policy</h3>
              </div>
            </div>
            <ActionForm
              action={updateProviderAccountAction}
              successMessage="Provider connection policy updated and audit evidence recorded."
              className="provider-drawer-form"
            >
              <input type="hidden" name="provider_account_id" value={account.id} />
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actor} />
              <div className="provider-form-grid">
                <label className="field">
                  <span>Account label</span>
                  <input
                    className="input"
                    name="account_label"
                    defaultValue={account.accountLabel}
                    disabled={!canOperate}
                  />
                </label>
                <label className="field">
                  <span>Owner actor</span>
                  <input
                    className="input"
                    name="owner_actor"
                    defaultValue={account.ownerActor ?? ""}
                    placeholder={actor}
                    disabled={!canOperate}
                  />
                </label>
              </div>
              <label className="field">
                <span>Granted provider scopes</span>
                <textarea
                  className="textarea"
                  name="scopes"
                  rows={2}
                  defaultValue={account.scopes.join("\n")}
                  disabled={!canOperate}
                />
              </label>
              <div className="provider-toggle-grid">
                {[
                  ["read_only", "Read-only", account.readOnly],
                  ["billing_readable", "Billing readable", account.billingReadable],
                  ["deploy_capable", "Deploy capable", account.deployCapable],
                  [
                    "destructive_actions_disabled",
                    "Destructive actions disabled",
                    account.destructiveActionsDisabled,
                  ],
                ].map(([name, label, checked]) => (
                  <label className="provider-toggle" key={String(name)}>
                    <input type="hidden" name={String(name)} value="false" />
                    <input
                      type="checkbox"
                      name={String(name)}
                      value="true"
                      defaultChecked={Boolean(checked)}
                      disabled={!canOperate}
                    />
                    <span>{String(label)}</span>
                  </label>
                ))}
              </div>
              <button
                className="button secondary"
                type="submit"
                disabled={!canOperate}
              >
                Save policy
              </button>
            </ActionForm>

            <div className="provider-control-row">
              <ProviderTestForm>
                <input
                  type="hidden"
                  name="provider_account_id"
                  value={account.id}
                />
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="actor" value={actor} />
                <button
                  className="button secondary"
                  type="submit"
                  disabled={!canOperate || account.status !== "active"}
                >
                  Test connection
                </button>
              </ProviderTestForm>
              <ProviderSyncForm>
                <input
                  type="hidden"
                  name="provider_account_id"
                  value={account.id}
                />
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="actor" value={actor} />
                <button
                  className="button secondary"
                  type="submit"
                  disabled={!canOperate || account.status !== "active"}
                >
                  Sync inventory
                </button>
              </ProviderSyncForm>
              {account.status === "disabled" ? (
                <ActionForm
                  action={enableProviderAccountAction}
                  successMessage="Provider connection enabled. Test it before running a sync."
                >
                  <input
                    type="hidden"
                    name="provider_account_id"
                    value={account.id}
                  />
                  <input type="hidden" name="project_id" value={projectId} />
                  <input type="hidden" name="actor" value={actor} />
                  <button className="button secondary" type="submit">
                    Enable
                  </button>
                </ActionForm>
              ) : (
                <ActionForm
                  action={disableProviderAccountAction}
                  successMessage="Provider connection disabled before adapter execution."
                >
                  <input
                    type="hidden"
                    name="provider_account_id"
                    value={account.id}
                  />
                  <input type="hidden" name="project_id" value={projectId} />
                  <input type="hidden" name="actor" value={actor} />
                  <button
                    className="button secondary"
                    type="submit"
                    disabled={!canOperate || account.status !== "active"}
                  >
                    Disable
                  </button>
                </ActionForm>
              )}
            </div>
          </section>
        ) : null}

        {canManage && account.status !== "revoked" ? (
          <section aria-labelledby="provider-rotate-title">
            <div className="provider-drawer-section-head">
              <div>
                <span className="eyebrow">Rotation</span>
                <h3 id="provider-rotate-title">Replace credential references</h3>
              </div>
            </div>
            <ActionForm
              action={rotateProviderCredentialsAction}
              successMessage="Credential references rotated with bounded overlap and audit lineage."
              className="provider-drawer-form"
            >
              <input type="hidden" name="provider_account_id" value={account.id} />
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actor} />
              {(slots.length > 0
                ? slots
                : Object.keys(account.credentialRefs).map((key) => ({
                    key,
                    secretKind: "provider_credential",
                    required: true,
                  }))
              ).map((slot) => (
                <label className="field" key={slot.key}>
                  <span>{titleCase(slot.key)} replacement SecretRef</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete="new-password"
                    name={`credential_ref__${slot.key}`}
                    placeholder="Write-only SecretRef pointer"
                    required={slot.required}
                    disabled={!available}
                  />
                </label>
              ))}
              <label className="field">
                <span>Leased-work overlap</span>
                <select
                  className="input"
                  name="overlap_minutes"
                  defaultValue="30"
                  disabled={!available}
                >
                  <option value="5">5 minutes</option>
                  <option value="15">15 minutes</option>
                  <option value="30">30 minutes</option>
                  <option value="60">60 minutes</option>
                </select>
              </label>
              <button
                className="button secondary"
                type="submit"
                disabled={!available}
              >
                Rotate credentials
              </button>
            </ActionForm>
          </section>
        ) : null}

        {canManage && account.status !== "revoked" ? (
          <section className="provider-danger-zone" aria-labelledby="revoke-title">
            <div className="provider-drawer-section-head">
              <div>
                <span className="eyebrow">Irreversible</span>
                <h3 id="revoke-title">Revoke provider access</h3>
              </div>
            </div>
            <div className="provider-risk-summary">
              <p>
                <strong>Impact:</strong> {accountResources.length} tracked resources,{" "}
                {formatMicros(observedSpend)} observed spend, and{" "}
                {accountChanges.length} ChangeSets remain as historical evidence.
              </p>
              <p>
                <strong>Approval:</strong> current actor must hold{" "}
                <code>providerops_manage</code>. <strong>Rollback:</strong> none;
                create a new connection and SecretRefs. <strong>Evidence:</strong>{" "}
                revocation event plus retained receipts.
              </p>
            </div>
            <ActionForm
              action={revokeProviderAccountAction}
              successMessage="Provider access permanently revoked; historical evidence retained."
              className="provider-drawer-form"
            >
              <input type="hidden" name="provider_account_id" value={account.id} />
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actor} />
              <input
                type="hidden"
                name="account_label"
                value={account.accountLabel}
              />
              <label className="field">
                <span>Type “{account.accountLabel}” to confirm</span>
                <input
                  className="input"
                  name="confirmation"
                  autoComplete="off"
                  required
                />
              </label>
              <button className="button danger" type="submit">
                Permanently revoke
              </button>
            </ActionForm>
          </section>
        ) : null}

        <section aria-labelledby="provider-evidence-title">
          <div className="provider-drawer-section-head">
            <div>
              <span className="eyebrow">Audit evidence</span>
              <h3 id="provider-evidence-title">Events and receipts</h3>
            </div>
            <a href={`/?area=ops&provider_account_id=${encodeURIComponent(account.id)}`}>
              Open audit
              <ArrowSquareOut size={14} aria-hidden />
            </a>
          </div>
          <div className="provider-evidence-list">
            {accountEvents.slice(0, 4).map((event) => (
              <div key={event.id}>
                <span>
                  <strong>{titleCase(event.eventType)}</strong>
                  <small>
                    {event.actor} · {formatDate(event.createdAt)}
                  </small>
                </span>
                <code>{event.id}</code>
              </div>
            ))}
            {accountReceipts.slice(0, 4).map((receipt) => (
              <div key={receipt.id}>
                <span>
                  <strong>{titleCase(receipt.action)}</strong>
                  <small>
                    {receipt.status} · {formatDate(receipt.createdAt)}
                  </small>
                </span>
                <code>{receipt.id}</code>
              </div>
            ))}
            {accountEvents.length === 0 && accountReceipts.length === 0 ? (
              <p>No account-specific event or receipt is present in this window.</p>
            ) : null}
          </div>
        </section>
      </aside>
    </>
  );
}

function ScopeFilters({
  feed,
  scope,
  setScope,
}: {
  feed: ProviderOpsFeed;
  scope: SavedScope;
  setScope: (scope: SavedScope) => void;
}) {
  const optionValues = (values: string[]) =>
    [...new Set(values.filter(Boolean))].sort();
  const regions = optionValues(
    feed.resources.map((resource) => metadataToken(resource, "region")),
  );
  const services = optionValues(
    feed.resources.map((resource) => metadataToken(resource, "service")),
  );
  const environments = optionValues(
    feed.resources.map((resource) => resource.environment),
  );
  const update = (key: keyof SavedScope, value: string) =>
    setScope({ ...scope, [key]: value });

  return (
    <div className="provider-scope-bar" aria-label="Saved resource scope filters">
      <span>
        <MagnifyingGlass size={15} aria-hidden />
        Saved scope
      </span>
      <label>
        <span className="sr-only">Provider</span>
        <select
          value={scope.provider}
          onChange={(event) => update("provider", event.target.value)}
        >
          <option value="">All providers</option>
          {optionValues(feed.resources.map((resource) => resource.provider)).map(
            (provider) => (
              <option key={provider} value={provider}>
                {titleCase(provider)}
              </option>
            ),
          )}
        </select>
      </label>
      <label>
        <span className="sr-only">Account</span>
        <select
          value={scope.account}
          onChange={(event) => update("account", event.target.value)}
        >
          <option value="">All accounts</option>
          {feed.providerAccounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.accountLabel}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">Environment</span>
        <select
          value={scope.environment}
          onChange={(event) => update("environment", event.target.value)}
        >
          <option value="">All environments</option>
          {environments.map((environment) => (
            <option key={environment} value={environment}>
              {titleCase(environment)}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">Region</span>
        <select
          value={scope.region}
          onChange={(event) => update("region", event.target.value)}
        >
          <option value="">All regions</option>
          {regions.map((region) => (
            <option key={region} value={region}>
              {region}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">Service</span>
        <select
          value={scope.service}
          onChange={(event) => update("service", event.target.value)}
        >
          <option value="">All services</option>
          {services.map((service) => (
            <option key={service} value={service}>
              {titleCase(service)}
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={() => setScope(EMPTY_SCOPE)}>
        Clear
      </button>
    </div>
  );
}

function ResourceView({
  feed,
  scope,
  setScope,
  onSelect,
}: {
  feed: ProviderOpsFeed;
  scope: SavedScope;
  setScope: (scope: SavedScope) => void;
  onSelect: (resource: ExternalResourceFeedItem) => void;
}) {
  const resources = feed.resources.filter((resource) =>
    matchesScope(resource, scope),
  );
  return (
    <div className="provider-view-panel">
      <ScopeFilters feed={feed} scope={scope} setScope={setScope} />
      {resources.length === 0 ? (
        <div className="provider-empty">
          <Database size={27} aria-hidden />
          <div>
            <strong>No resources in this scope</strong>
            <p>Run a read-only sync or clear filters to inspect other inventory.</p>
          </div>
        </div>
      ) : (
        <div className="provider-table-wrap">
          <table className="provider-table">
            <caption className="sr-only">Provider resource inventory</caption>
            <thead>
              <tr>
                <th scope="col">Resource</th>
                <th scope="col">Provider / account</th>
                <th scope="col">Location</th>
                <th scope="col">Ownership</th>
                <th scope="col">Health</th>
                <th scope="col">Cost</th>
                <th scope="col">
                  <span className="sr-only">Open details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {resources.map((resource) => {
                const account = feed.providerAccounts.find(
                  (item) => item.id === resource.providerAccountId,
                );
                return (
                  <tr key={resource.id}>
                    <td>
                      <strong>{resource.name}</strong>
                      <small>{titleCase(resource.resourceType)}</small>
                    </td>
                    <td>
                      <strong>{titleCase(resource.provider)}</strong>
                      <small>{account?.accountLabel ?? "Unlinked account"}</small>
                    </td>
                    <td>
                      <strong>{metadataToken(resource, "region") || "Global"}</strong>
                      <small>
                        {titleCase(resource.environment)} ·{" "}
                        {titleCase(metadataToken(resource, "service") || "general")}
                      </small>
                    </td>
                    <td>
                      <StatusBadge status={resource.ownership} />
                      <small>{resource.ownerRole ?? "No owner role"}</small>
                    </td>
                    <td>
                      <StatusBadge status={resource.healthStatus} />
                      <small>Seen {formatDate(resource.lastSeenAt)}</small>
                    </td>
                    <td>
                      <strong>
                        {formatMicros(
                          resource.actualMonthlyUsdMicros ??
                            resource.estimatedMonthlyUsdMicros ??
                            0,
                        )}
                      </strong>
                      <small>{titleCase(resource.costConfidence)}</small>
                    </td>
                    <td>
                      <button
                        className="provider-row-action"
                        type="button"
                        onClick={() => onSelect(resource)}
                        aria-label={`Open ${resource.name} resource details`}
                      >
                        Inspect
                        <CaretRight size={14} aria-hidden />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ResourceDrawer({
  resource,
  feed,
  onClose,
}: {
  resource: ExternalResourceFeedItem;
  feed: ProviderOpsFeed;
  onClose: () => void;
}) {
  const account = feed.providerAccounts.find(
    (item) => item.id === resource.providerAccountId,
  );
  const receipt = feed.resourceReceipts.find(
    (item) => item.id === resource.receiptId,
  );
  return (
    <>
      <button
        type="button"
        className="provider-drawer-scrim"
        onClick={onClose}
        aria-label="Close resource details"
      />
      <aside
        className="provider-drawer resource"
        role="dialog"
        aria-modal="false"
        aria-labelledby="resource-drawer-title"
      >
        <header>
          <div>
            <span className="eyebrow">{titleCase(resource.resourceType)}</span>
            <h2 id="resource-drawer-title">{resource.name}</h2>
            <p>
              {titleCase(resource.provider)} ·{" "}
              {account?.accountLabel ?? "Unlinked provider account"}
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close details">
            <X size={19} aria-hidden />
          </button>
        </header>
        <div className="provider-drawer-status">
          <StatusBadge status={resource.state} />
          <StatusBadge status={resource.healthStatus} />
          <StatusBadge status={resource.ownership} />
        </div>
        <dl className="provider-resource-detail">
          <div>
            <dt>External ID</dt>
            <dd>{resource.externalId}</dd>
          </div>
          <div>
            <dt>Environment</dt>
            <dd>{titleCase(resource.environment)}</dd>
          </div>
          <div>
            <dt>Region / service</dt>
            <dd>
              {metadataToken(resource, "region") || "Global"} /{" "}
              {metadataToken(resource, "service") || "General"}
            </dd>
          </div>
          <div>
            <dt>Owner</dt>
            <dd>{resource.ownerRole ?? "Unassigned"}</dd>
          </div>
          <div>
            <dt>Cost</dt>
            <dd>
              {formatMicros(
                resource.actualMonthlyUsdMicros ??
                  resource.estimatedMonthlyUsdMicros ??
                  0,
              )}{" "}
              · {titleCase(resource.costConfidence)}
            </dd>
          </div>
          <div>
            <dt>Last seen</dt>
            <dd>{formatDate(resource.lastSeenAt)}</dd>
          </div>
        </dl>
        <section>
          <span className="eyebrow">Durable links</span>
          <div className="provider-evidence-list">
            {[
              ["Task", resource.taskId],
              ["Deployment", resource.deploymentTargetId],
              ["Budget", resource.budgetAllocationId],
              ["Receipt", receipt?.id ?? resource.receiptId],
            ].map(([label, value]) => (
              <div key={label}>
                <strong>{label}</strong>
                <code>{value ?? "Not linked"}</code>
              </div>
            ))}
          </div>
        </section>
      </aside>
    </>
  );
}

function HealthView({ feed }: { feed: ProviderOpsFeed }) {
  return (
    <div className="provider-view-panel provider-split-view">
      <section aria-labelledby="provider-health-heading">
        <div className="provider-view-heading">
          <div>
            <span className="eyebrow">Provider status</span>
            <h3 id="provider-health-heading">Health and freshness</h3>
          </div>
          <span>{feed.providerHealth.length} observations</span>
        </div>
        <div className="provider-list">
          {feed.providerHealth.map((health) => {
            const account = feed.providerAccounts.find(
              (item) => item.id === health.providerAccountId,
            );
            return (
              <div key={health.id}>
                <span>
                  <strong>{account?.accountLabel ?? titleCase(health.provider)}</strong>
                  <small>
                    Rate limit {health.rateLimitStatus} · cap{" "}
                    {health.usageCapStatus} · {formatDate(health.lastCheckedAt)}
                  </small>
                </span>
                <StatusBadge
                  status={health.apiAvailable ? health.status : "unavailable"}
                />
              </div>
            );
          })}
          {feed.providerHealth.length === 0 ? (
            <div className="provider-empty compact">
              <Gauge size={22} aria-hidden />
              <p>No provider health observations yet.</p>
            </div>
          ) : null}
        </div>
      </section>
      <section aria-labelledby="provider-capacity-heading">
        <div className="provider-view-heading">
          <div>
            <span className="eyebrow">Worker capacity</span>
            <h3 id="provider-capacity-heading">Scaling posture</h3>
          </div>
          <span>{feed.summary.desiredWorkerCount} desired workers</span>
        </div>
        <div className="provider-list">
          {feed.workerPools.map((pool) => (
            <div key={pool.id}>
              <span>
                <strong>{pool.poolKey}</strong>
                <small>
                  {titleCase(pool.kind)} · current {pool.currentWorkers} · desired{" "}
                  {pool.desiredWorkers} · max {pool.maxWorkers}
                </small>
              </span>
              <StatusBadge status={pool.status} />
            </div>
          ))}
          {feed.scalePlans.slice(0, 4).map((plan) => (
            <div key={plan.id}>
              <span>
                <strong>{titleCase(plan.triggerReason)}</strong>
                <small>
                  {plan.desiredWorkersBefore} → {plan.desiredWorkersAfter} ·{" "}
                  {formatMicros(plan.estimatedHourlyDeltaUsdMicros)}/hour
                </small>
              </span>
              <StatusBadge status={plan.status} />
            </div>
          ))}
          {feed.workerPools.length === 0 && feed.scalePlans.length === 0 ? (
            <div className="provider-empty compact">
              <Gauge size={22} aria-hidden />
              <p>
                No worker pools or scale plans are recorded. On community you can
                still register local pools and scale plans against your own infra
                once keys and targets are connected.
              </p>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

export function costByProvider(costs: CostEventFeedItem[]) {
  const totals = new Map<string, number>();
  for (const cost of costs) {
    totals.set(cost.provider, (totals.get(cost.provider) ?? 0) + cost.amountUsdMicros);
  }
  return [...totals.entries()]
    .map(([provider, amount]) => ({ provider, amount }))
    .sort((left, right) => right.amount - left.amount);
}

function FinOpsView({ feed }: { feed: ProviderOpsFeed }) {
  const totals = costByProvider(feed.costs);
  const max = Math.max(...totals.map((item) => item.amount), 1);
  return (
    <div className="provider-view-panel provider-split-view">
      <section aria-labelledby="provider-spend-heading">
        <div className="provider-view-heading">
          <div>
            <span className="eyebrow">Observed spend</span>
            <h3 id="provider-spend-heading">Provider distribution</h3>
          </div>
          <strong>{formatMicros(feed.summary.spendUsdMicros)}</strong>
        </div>
        {totals.length > 0 ? (
          <div className="provider-cost-chart" role="img" aria-label="Observed cost by provider">
            {totals.map((item) => (
              <div key={item.provider}>
                <span>{titleCase(item.provider)}</span>
                <div>
                  <i
                    style={{ width: `${Math.max((item.amount / max) * 100, 2)}%` }}
                  />
                </div>
                <strong>{formatMicros(item.amount)}</strong>
              </div>
            ))}
          </div>
        ) : (
          <div className="provider-empty compact">
            <ChartBar size={22} aria-hidden />
            <p>No cost events are available for this project.</p>
          </div>
        )}
        <p className="provider-helper">
          Every value retains its confidence label. Estimates are never presented
          as reconciled actuals.
        </p>
      </section>
      <section aria-labelledby="provider-finops-evidence-heading">
        <div className="provider-view-heading">
          <div>
            <span className="eyebrow">Finance controls</span>
            <h3 id="provider-finops-evidence-heading">Anomalies and reconciliation</h3>
          </div>
          <span>{feed.summary.openCostAnomalies} open anomalies</span>
        </div>
        <div className="provider-list">
          {feed.costAnomalies.slice(0, 5).map((anomaly) => (
            <div key={anomaly.id}>
              <span>
                <strong>{anomaly.summary}</strong>
                <small>
                  {titleCase(anomaly.provider)} · observed{" "}
                  {formatMicros(anomaly.amountUsdMicros)} · threshold{" "}
                  {formatMicros(anomaly.thresholdUsdMicros)}
                </small>
              </span>
              <StatusBadge status={anomaly.status} />
            </div>
          ))}
          {feed.reconciliationRuns.slice(0, 5).map((run) => (
            <div key={run.id}>
              <span>
                <strong>{titleCase(run.provider ?? "all providers")}</strong>
                <small>
                  {run.resourcesSeen} resources · {run.costsSeen} costs ·{" "}
                  {formatDate(run.startedAt)}
                </small>
              </span>
              <StatusBadge status={run.status} />
            </div>
          ))}
          {feed.costAnomalies.length === 0 &&
          feed.reconciliationRuns.length === 0 ? (
            <div className="provider-empty compact">
              <ShieldCheck size={22} aria-hidden />
              <p>No anomalies or reconciliation runs are recorded.</p>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

export function ProviderConfigurationSuite({
  feed,
  projectId,
  actor,
  canManage,
  events,
}: {
  feed: ProviderOpsFeed;
  projectId: string;
  actor: string;
  canManage: boolean;
  events: ActivityFeedItem[];
}) {
  const [view, setView] = useState<ProviderView>("connections");
  const [scope, setScopeState] = useState<SavedScope>(EMPTY_SCOPE);
  const [selectedProvider, setSelectedProvider] = useState(
    feed.providerDefinitions[0]?.provider ?? "",
  );
  const [selectedAccountId, setSelectedAccountId] = useState<string>();
  const [selectedResourceId, setSelectedResourceId] = useState<string>();

  useEffect(() => {
    const storedView = window.localStorage.getItem("boss.providerops.view");
    const queryView = new URLSearchParams(window.location.search).get("infra_view");
    const candidate = queryView ?? storedView;
    if (VIEWS.some((item) => item.id === candidate)) {
      setView(candidate as ProviderView);
    }
    const storedScope = window.localStorage.getItem("boss.providerops.scope");
    if (storedScope) {
      try {
        setScopeState({ ...EMPTY_SCOPE, ...JSON.parse(storedScope) });
      } catch {
        window.localStorage.removeItem("boss.providerops.scope");
      }
    }
  }, []);

  const setScope = (next: SavedScope) => {
    setScopeState(next);
    window.localStorage.setItem("boss.providerops.scope", JSON.stringify(next));
  };

  const changeView = (next: ProviderView) => {
    setView(next);
    window.localStorage.setItem("boss.providerops.view", next);
    const url = new URL(window.location.href);
    url.searchParams.set("infra_view", next);
    window.history.replaceState(null, "", url.toString());
  };

  const selectedAccount = useMemo(
    () => feed.providerAccounts.find((account) => account.id === selectedAccountId),
    [feed.providerAccounts, selectedAccountId],
  );
  const selectedResource = useMemo(
    () => feed.resources.find((resource) => resource.id === selectedResourceId),
    [feed.resources, selectedResourceId],
  );
  const definition = feed.providerDefinitions.find(
    (item) => item.provider === selectedProvider,
  );
  const revokedCount = feed.providerAccounts.filter(
    (account) => account.status === "revoked",
  ).length;
  const disabledCount = feed.providerAccounts.filter(
    (account) => account.status === "disabled",
  ).length;

  if (!feed.apiReachable) {
    return (
      <div className="provider-suite-error" role="alert">
        <Warning size={24} aria-hidden />
        <div>
          <strong>Provider configuration unavailable</strong>
          <p>
            {feed.blockedReason ??
              "The ProviderOps configuration APIs could not be reached."}
          </p>
        </div>
      </div>
    );
  }

  const editionLabel = feed.editionId
    ? titleCase(feed.editionId)
    : "Community";
  const hasEditionLimits = (feed.editionLimitations?.length ?? 0) > 0;
  const hasPartialFailures = feed.partialFailures.length > 0;
  const isCommunity =
    !feed.editionId || String(feed.editionId).toLowerCase() === "community";

  return (
    <div className="provider-suite" id="provider-connections">
      {isCommunity ? (
        <div className="provider-notice" role="status">
          <Lock size={17} aria-hidden />
          <span>
            {editionLabel} is fully functional when operators bring their own
            keys: attach model and infra API keys, sync inventory,
            plan/approve/apply ChangeSets, and run local worker pools against
            infrastructure you manage. Self-hosted SSO is included; hosted
            Account Connect, marketplace, and managed fleets stay on Cloud /
            Enterprise.
          </span>
        </div>
      ) : null}
      {hasEditionLimits ? (
        <div className="provider-notice" role="status">
          <Lock size={17} aria-hidden />
          <span>
            Edition note
            {feed.editionLimitations.length === 1
              ? `: ${feed.editionLimitations[0]}`
              : feed.editionLimitations.length > 1
                ? `: ${feed.editionLimitations.join("; ")}`
                : ""}
            .
          </span>
        </div>
      ) : null}
      {hasPartialFailures ? (
        <div className="provider-notice warn" role="status">
          <Warning size={17} aria-hidden />
          <span>
            Partial data (secondary APIs): {feed.partialFailures.join("; ")}.
            Catalog and connections remain operational where loaded.
          </span>
        </div>
      ) : null}
      {!canManage ? (
        <div className="provider-notice" role="note">
          <Lock size={17} aria-hidden />
          <span>
            Read-only view. Actor <strong>{actor}</strong> does not hold{" "}
            <code>providerops_manage</code>; mutation controls are unavailable.
          </span>
        </div>
      ) : null}

      <div className="provider-suite-summary" aria-label="Provider configuration summary">
        <div>
          <span>Connections</span>
          <strong>{feed.providerAccounts.length}</strong>
          <small>
            {disabledCount} disabled · {revokedCount} revoked
          </small>
        </div>
        <div>
          <span>Available adapters</span>
          <strong>
            {
              feed.providerDefinitions.filter(
                (item) => item.adapterStatus === "available",
              ).length
            }
          </strong>
          <small>{feed.providerDefinitions.length} cataloged</small>
        </div>
        <div>
          <span>Resources</span>
          <strong>{feed.summary.resourceCount}</strong>
          <small>{feed.summary.unmanagedResourceCount} unmanaged</small>
        </div>
        <div>
          <span>Health</span>
          <strong>{feed.summary.healthyProviders}</strong>
          <small>{feed.providerHealth.length} observations</small>
        </div>
        <div>
          <span>Observed spend</span>
          <strong>{formatMicros(feed.summary.spendUsdMicros)}</strong>
          <small>{feed.summary.openCostAnomalies} open anomalies</small>
        </div>
      </div>

      <nav className="provider-subnav" aria-label="System configuration sections">
        {VIEWS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              className={view === item.id ? "active" : ""}
              aria-current={view === item.id ? "page" : undefined}
              onClick={() => changeView(item.id)}
            >
              <Icon size={17} weight={view === item.id ? "fill" : "regular"} aria-hidden />
              <span>
                <strong>{item.label}</strong>
                <small>{item.detail}</small>
              </span>
            </button>
          );
        })}
      </nav>

      {view === "connections" ? (
        <div className="provider-view-panel">
          <div className="provider-view-heading">
            <div>
              <span className="eyebrow">Connections / Providers</span>
              <h3>Infrastructure provider catalog</h3>
              <p>
                Available adapters can be configured. Planned adapters remain
                visible but fail closed.
              </p>
            </div>
            <span>Project {projectId}</span>
          </div>
          <ProviderCatalog
            definitions={feed.providerDefinitions}
            selectedProvider={selectedProvider}
            onSelect={setSelectedProvider}
          />
          <ConnectionCreate
            projectId={projectId}
            actor={actor}
            canManage={canManage}
            definition={definition}
          />
          <div className="provider-view-heading accounts">
            <div>
              <span className="eyebrow">Configured accounts</span>
              <h3>Credential and authority posture</h3>
            </div>
            <span>Refreshed {formatDate(feed.refreshedAt)}</span>
          </div>
          <ProviderAccountTable
            feed={feed}
            canManage={canManage}
            onSelect={(account) => setSelectedAccountId(account.id)}
          />
        </div>
      ) : null}
      {view === "resources" ? (
        <ResourceView
          feed={feed}
          scope={scope}
          setScope={setScope}
          onSelect={(resource) => setSelectedResourceId(resource.id)}
        />
      ) : null}
      {view === "health" ? <HealthView feed={feed} /> : null}
      {view === "finops" ? <FinOpsView feed={feed} /> : null}

      <footer className="provider-suite-footer">
        <span>
          <CheckCircle size={15} weight="fill" aria-hidden />
          Secrets stay behind SecretRefs. Provider writes still require ChangeSet,
          budget, approval, rollback, and ResourceReceipt.
        </span>
        <a href="/?area=ops">
          Audit & events
          <ArrowSquareOut size={14} aria-hidden />
        </a>
      </footer>

      {selectedAccount ? (
        <AccountDrawer
          account={selectedAccount}
          definition={definitionForAccount(feed.providerDefinitions, selectedAccount)}
          feed={feed}
          events={events}
          projectId={projectId}
          actor={actor}
          canManage={canManage}
          onClose={() => setSelectedAccountId(undefined)}
        />
      ) : null}
      {selectedResource ? (
        <ResourceDrawer
          resource={selectedResource}
          feed={feed}
          onClose={() => setSelectedResourceId(undefined)}
        />
      ) : null}
    </div>
  );
}
