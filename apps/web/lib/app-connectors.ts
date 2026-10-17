/**
 * APP › CONNECTORS AND SETTINGS, the rules (BOSSAPP-568). What the redesigned Setup sheet draws,
 * from the reads it already had (an app's templates, secrets, setup guide, demo logins) — pure, so
 * every rule is tested without a screen:
 *
 *   - ONE CARD PER CONNECTOR. The app's templates list has a row per build attempt (HelpDesk AI had
 *     "Login with Google, GitHub or BOSS" twelve times). A card is a connector key; its state is the
 *     newest attempt's; the older ones fold into "N earlier build attempts".
 *   - A ROW PER SIGN-IN PROVIDER (Google, GitHub, BOSS, and the demo login when the app has one),
 *     with its status and what to do, instead of a flat list of env names with Build/Preview/Deploy
 *     chips. A provider is set up once its client id has a stored value for preview or deploy —
 *     THE SAME RULE as the Apps row's chip (engine/boss_engine/templates/setup_needs.py).
 *   - A WARNING ONLY WHEN AN ADDRESS CANNOT WORK: an address inside BOSS (api:3001), http:// or a
 *     bare IP for Google, no public address at all.
 *   - SETTINGS SPLIT: what BOSS manages (generated, a test double) vs what a person set.
 *
 * Names and states only: no value ever passes through here.
 */
import type { AppSecrets, Applied, DeclaredSecret, Guide, SecretBinding, SetupAddresses, SignInProvider } from "./templates";

// ---- one card per connector ------------------------------------------------------------------

export type ConnectorCard = {
  key: string; name: string; version: string; stack: string;
  /** The newest attempt: the card's state. */
  latest: Applied;
  /** Every older attempt, newest first (folded on the card). */
  earlier: Applied[];
  /** Some attempt is in the app or on its way (not every one failed or was abandoned). */
  inApp: boolean;
  providers: SignInProvider[];
  /** This connector's own secret names. */
  secrets: string[];
};

const GONE = new Set(["failed", "abandoned"]);

/** The app's attempts (newest first, as the API lists them) as one card per connector key. */
export function connectorCards(applied: Applied[]): ConnectorCard[] {
  const byKey = new Map<string, Applied[]>();
  const sorted = [...applied].sort((a, b) => (b.createdAt || "").localeCompare(a.createdAt || ""));
  for (const a of sorted) {
    const k = a.key || a.name;
    byKey.set(k, [...(byKey.get(k) ?? []), a]);
  }
  return [...byKey.entries()].map(([key, all]) => {
    const latest = all[0];
    const providers: SignInProvider[] = [];
    const secrets: string[] = [];
    for (const a of all) {
      for (const p of a.signInProviders) if (!providers.some((x) => x.provider === p.provider)) providers.push(p);
      for (const s of a.secrets) if (!secrets.includes(s)) secrets.push(s);
    }
    return { key, name: latest.name, version: latest.version, stack: latest.stack, latest, earlier: all.slice(1),
             inApp: all.some((a) => !GONE.has(a.status)), providers, secrets };
  });
}

// ---- provider rows ---------------------------------------------------------------------------

const PROVIDER_NAME: Record<string, string> = { google: "Google", github: "GitHub", boss: "BOSS" };
export const providerName = (p: string): string => PROVIDER_NAME[p] ?? (p ? p[0].toUpperCase() + p.slice(1) : p);

/** A stored value a run can use (a stub is the build's fake provider: not one). */
const hasValue = (b: SecretBinding | undefined): boolean => Boolean(b && b.mode !== "stub" && b.mode);

/** One env's keys for a provider: both set, a live key not yet approved, or none. */
export type EnvKeys = "set" | "awaiting" | "none";

function bindingOf(bindings: SecretBinding[], name: string, env: string): SecretBinding | undefined {
  return bindings.find((b) => b.name === name && b.env === env);
}

export function envKeys(bindings: SecretBinding[], p: SignInProvider, env: "preview" | "deploy"): EnvKeys {
  const id = bindingOf(bindings, p.id, env);
  const secret = bindingOf(bindings, p.secret, env);
  if (!hasValue(id) || !hasValue(secret)) return "none";
  if ([id, secret].some((b) => b?.mode === "live" && !b.liveApproved)) return "awaiting";
  return "set";
}

/** THE ROW CHIP'S RULE (setup_needs.py): no stored client id for preview or deploy. */
export function providerNeedsYou(bindings: SecretBinding[], p: SignInProvider): boolean {
  return !hasValue(bindingOf(bindings, p.id, "preview")) && !hasValue(bindingOf(bindings, p.id, "deploy"));
}

export type ProviderStatus = "ready" | "preview_only" | "deploy_only" | "awaiting" | "needs_keys" | "cant_work";
export type ProviderRow = {
  provider: string; name: string; spec: SignInProvider;
  preview: EnvKeys; deploy: EnvKeys;
  status: ProviderStatus; word: string; tone: "green" | "amber" | "red";
  needsYou: boolean;
};

export function providerRow(o: { spec: SignInProvider; bindings: SecretBinding[]; bossPublicUrl: string | null; deployLabel?: string }): ProviderRow {
  const { spec, bindings } = o;
  const dl = o.deployLabel || "Deploy";
  const preview = envKeys(bindings, spec, "preview");
  const deploy = envKeys(bindings, spec, "deploy");
  const needsYou = providerNeedsYou(bindings, spec);
  const base = { provider: spec.provider, name: providerName(spec.provider), spec, preview, deploy, needsYou };
  if (spec.provider === "boss" && !o.bossPublicUrl && preview === "none" && deploy === "none") {
    return { ...base, status: "cant_work", word: "Can't work yet", tone: "red" };
  }
  if (preview === "set" && deploy === "set") return { ...base, status: "ready", word: "Ready", tone: "green" };
  if (preview === "awaiting" || deploy === "awaiting") return { ...base, status: "awaiting", word: "Live key awaiting approval", tone: "amber" };
  if (preview === "set") return { ...base, status: "preview_only", word: `Preview only · ${dl} needs keys`, tone: "amber" };
  if (deploy === "set") return { ...base, status: "deploy_only", word: `${dl} only`, tone: "amber" };
  return { ...base, status: "needs_keys", word: spec.provider === "boss" ? "Not connected" : "Needs keys", tone: "amber" };
}

// ---- demo login ------------------------------------------------------------------------------

/** run_env.is_demo_login_name, the same pattern: DEMO_PASSWORD, ADMIN_PASSWORD, SEED_*_PASSWORD… */
const DEMO_LOGIN = /^(?:[A-Z0-9]+_)*(?:DEMO|SEED|ADMIN|DEFAULT_ADMIN|INITIAL_ADMIN|TEST_USER)(?:_[A-Z0-9]+)*_PASSWORD$/;
const NOT_A_DEMO_LOGIN = /(?:^|_)(?:DB|DATABASE|POSTGRES|PG|MYSQL|REDIS|SMTP|MAIL|SMTPS)(?:_|$)/;
export const isDemoLoginName = (n: string): boolean => DEMO_LOGIN.test(n) && !NOT_A_DEMO_LOGIN.test(n);

/** The demo login passwords BOSS generated for this app (names only). */
export function demoLoginNames(bindings: SecretBinding[]): string[] {
  return [...new Set(bindings.filter((b) => b.mode === "generated" && isDemoLoginName(b.name)).map((b) => b.name))].sort();
}

// ---- keys (a connector's own secrets a person must give: a payment API key) ------------------

/** A connector's keys: its declared secrets that need a person (test default `none`), no
 *  provider's client id or secret. */
export function connectorKeys(card: ConnectorCard, declared: DeclaredSecret[]): DeclaredSecret[] {
  const owned = new Set(card.providers.flatMap((p) => [p.id, p.secret]));
  // An API that does not say which secrets are the connector's (an older one): all of them.
  const mine = (n: string) => !card.secrets.length || card.secrets.includes(n);
  return declared.filter((d) => mine(d.name) && !owned.has(d.name) && d.testDefault === "none");
}

/** THE ROW CHIP'S RULE for a key (setup_needs.py): no stored value for preview or deploy. */
export function keyNeedsYou(bindings: SecretBinding[], name: string): boolean {
  return !hasValue(bindingOf(bindings, name, "preview")) && !hasValue(bindingOf(bindings, name, "deploy"));
}

// ---- the card's header -----------------------------------------------------------------------

export type WhereCell = { env: string; word: string; tone: "green" | "amber" | "muted" };

/** "Where:" — what the connector runs on in the preview and on the deploy, in two cells. */
export function whereLine(o: { card: ConnectorCard; rows: ProviderRow[]; keys: DeclaredSecret[]; bindings: SecretBinding[];
  hasDemo: boolean; deployLabel?: string }): WhereCell[] {
  const dl = o.deployLabel || "Deploy";
  if (o.card.providers.length) {
    const pv = o.rows.some((r) => r.preview === "set") ? { word: "test keys", tone: "green" as const }
      : o.hasDemo ? { word: "demo login", tone: "green" as const } : { word: "test sign-in only", tone: "muted" as const };
    const dp = o.rows.some((r) => r.deploy === "set") ? { word: "keys set", tone: "green" as const }
      : o.rows.some((r) => r.deploy === "awaiting") ? { word: "awaiting approval", tone: "amber" as const }
      : { word: "needs keys", tone: "amber" as const };
    return [{ env: "Preview", ...pv }, { env: dl, ...dp }];
  }
  const set = (env: string) => o.keys.every((k) => hasValue(bindingOf(o.bindings, k.name, env)));
  if (!o.keys.length) return [{ env: "Preview", word: "test mode", tone: "green" }, { env: dl, word: "ready", tone: "green" }];
  return [
    { env: "Preview", ...(set("preview") ? { word: "test keys", tone: "green" as const } : { word: "needs keys", tone: "amber" as const }) },
    { env: dl, ...(set("deploy") ? { word: "keys set", tone: "green" as const } : { word: "needs keys", tone: "amber" as const }) },
  ];
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The card's one line. */
export function summaryLine(o: { card: ConnectorCard; rows: ProviderRow[]; keys: DeclaredSecret[]; bindings: SecretBinding[]; hasDemo: boolean }): string {
  if (!o.card.inApp) return "Not in the app: every build of it stopped. Add it again from Add a feature.";
  if (o.card.providers.length) {
    const n = o.rows.filter((r) => r.needsYou).length;
    if (!n) return `Real sign-in is set up for ${o.rows.map((r) => r.name).join(", ")}.`;
    const now = o.hasDemo ? "Works in the preview now with the demo login." : "Builds and previews sign in through a test provider.";
    return `${now} Real sign-in needs ${plural(n, "provider")} set up.`;
  }
  const missing = o.keys.filter((k) => k.required !== false && keyNeedsYou(o.bindings, k.name)).length;
  return missing ? `Needs ${plural(missing, "key")} before it works for real.` : "Its keys are set.";
}

// ---- addresses -------------------------------------------------------------------------------

const IPV4 = /^\d{1,3}(?:\.\d{1,3}){3}$/;
function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^\[|\]$/g, ""); } catch { return ""; }
}
const isLoopback = (h: string) => h === "localhost" || h.endsWith(".localhost") || h === "::1" || /^127\./.test(h);
/** An address only BOSS's own containers resolve: one label, not localhost (api, web, db). */
export function isInternalHost(h: string): boolean {
  return Boolean(h) && !isLoopback(h) && !IPV4.test(h) && !h.includes(".") && !h.includes(":");
}
/** An address a person may be shown to use (never api:3001). */
export function isUsableAddress(url: string | null | undefined): url is string {
  if (!url || !/^https?:\/\//.test(url)) return false;
  return !isInternalHost(hostOf(url));
}

/** Why `url` cannot work as `provider`'s callback ("" when it can). */
export function addressProblem(url: string, provider: string): string {
  const host = hostOf(url);
  if (!host) return "";
  if (isInternalHost(host)) return `This address (${host}) is inside BOSS: ${providerName(provider)} cannot reach it.`;
  if (provider === "google" && !isLoopback(host)) {
    const http = url.startsWith("http://");
    const ip = IPV4.test(host);
    if (http && ip) return "Google accepts only https:// callbacks on a domain name (localhost aside). Give the deploy a domain with https before Google sign-in can work there.";
    if (http) return "Google accepts only https:// callbacks (localhost aside). Give the deploy an https address before Google sign-in can work there.";
    if (ip) return "Google does not accept a bare IP address as a callback. Give the deploy a domain name.";
  }
  return "";
}

/** The preview's public address in words when BOSS cannot give one. */
export function previewReachWords(a: SetupAddresses): string {
  if (a.preview.reach === "inside_only" || (a.preview.url && !isUsableAddress(a.preview.url))) return "The preview is reachable inside BOSS only: set BOSS's public address.";
  if (!a.preview.running) return "Start a preview to get its address.";
  return "";
}

export type Callback = { env: "preview" | "deploy"; url: string; problem: string };

/** Where `provider` must send people back, per env, from the app's setup guide. */
export function providerCallbacks(o: { provider: string; guide: Guide | undefined; addresses: SetupAddresses }): { callbacks: Callback[]; missing: string[] } {
  const copy = o.guide?.copy ?? {};
  const out: Callback[] = [];
  const missing: string[] = [];
  const redirect = copy.redirect_uri ?? "";
  const previewBase = isUsableAddress(o.addresses.preview.url) ? o.addresses.preview.url.replace(/\/$/, "") : "";
  const pv = redirect && !redirect.startsWith("(") && !redirect.includes("<preview>") ? `${redirect}/${o.provider}`
    : previewBase ? `${previewBase}/auth/callback/${o.provider}` : "";
  if (pv && isUsableAddress(pv)) out.push({ env: "preview", url: pv, problem: addressProblem(pv, o.provider) });
  else missing.push(previewReachWords(o.addresses) || "The preview has no public address.");
  const dp = copy[`callback_url:${o.provider}`];
  if (dp && isUsableAddress(dp)) out.push({ env: "deploy", url: dp, problem: addressProblem(dp, o.provider) });
  else if (dp && !dp.startsWith("(")) out.push({ env: "deploy", url: dp, problem: addressProblem(dp, o.provider) || "This address cannot be reached from outside BOSS." });
  else missing.push("Deploy the app to get its callback address.");
  return { callbacks: out, missing };
}

// ---- settings --------------------------------------------------------------------------------

export type SettingRow = {
  name: string; purpose: string;
  /** Each env it has a binding for, and how it got its value. */
  envs: { env: string; mode: string }[];
  /** BOSS made it (generated, a test double) — or will, for a declared one with no value yet. */
  managed: boolean;
  /** How BOSS made it, in words ("Generated by BOSS"). */
  how: string;
  demo: boolean;
  /** A connector declares it (a person may replace it with their own value). */
  declared: boolean;
};

/** Settings: every secret that is no connector's key or provider, split into BOSS's and yours. */
export function settingsSplit(o: { secrets: AppSecrets; cards: ConnectorCard[] }): { managed: SettingRow[]; yours: SettingRow[] } {
  const owned = new Set<string>();
  for (const c of o.cards) {
    for (const p of c.providers) { owned.add(p.id); owned.add(p.secret); if (p.provider === "boss") owned.add("OIDC_BOSS_ISSUER"); }
    for (const k of connectorKeys(c, o.secrets.declared)) owned.add(k.name);
  }
  const names = new Set<string>();
  for (const d of o.secrets.declared) if (!owned.has(d.name)) names.add(d.name);
  for (const b of o.secrets.bindings) if (!owned.has(b.name)) names.add(b.name);
  const managed: SettingRow[] = [];
  const yours: SettingRow[] = [];
  for (const name of [...names].sort()) {
    const d = o.secrets.declared.find((x) => x.name === name);
    const mine = o.secrets.bindings.filter((b) => b.name === name);
    const personal = mine.some((b) => b.mode === "test" || b.mode === "live");
    const stub = mine.length ? mine.every((b) => b.mode === "stub") : d?.testDefault === "stub";
    const row: SettingRow = {
      name, purpose: d?.purpose ?? "", envs: mine.map((b) => ({ env: b.env, mode: b.mode })),
      managed: !personal, how: stub ? "Test double" : isDemoLoginName(name) ? "Generated by BOSS · demo login" : "Generated by BOSS",
      demo: isDemoLoginName(name), declared: Boolean(d),
    };
    (personal ? yours : managed).push(row);
  }
  return { managed, yours };
}

// ---- the Apps row's chip ---------------------------------------------------------------------

export type SetupNeeds = { providers: string[]; keys: string[] };

export function parseSetupNeeds(v: unknown): SetupNeeds | undefined {
  if (!v || typeof v !== "object") return undefined;
  const o = v as Record<string, unknown>;
  const list = (x: unknown) => (Array.isArray(x) ? x.map(String).filter(Boolean) : []);
  const out = { providers: list(o.providers), keys: list(o.keys) };
  return out.providers.length || out.keys.length ? out : undefined;
}

/** The row's amber chip — only when a connector needs the operator. */
export function setupChip(n: SetupNeeds | undefined | null): { text: string; tip: string } | null {
  if (!n) return null;
  if (n.providers.length) {
    const names = n.providers.map(providerName);
    return { text: `Set up sign-in · ${plural(names.length, "provider")}`,
             tip: `${names.join(", ")} ${names.length === 1 ? "needs" : "need"} keys before real people can sign in` };
  }
  if (n.keys.length) return { text: `Set up · ${plural(n.keys.length, "key")}`, tip: `Needs ${n.keys.join(", ")}` };
  return null;
}
