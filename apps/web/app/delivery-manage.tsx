"use client";

/**
 * Managing delivery: register app destinations and hold their credential references.
 *
 * Target registration remains available while direct host preparation, Community deployment,
 * and destructive history actions await approved and recoverable workflows. A secret VALUE is
 * never displayed; the only key material shown is a public half.
 */

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  bindSecretAction, createTargetAction, deployObservabilityAction,
  getDeliveryDefaultsAction, listRefsAction, listRepoAppsAction, listReposAction,
  updateTargetAction,
  deployTargetAction,
  generateKeypairAction, putSecretAction,
  setAutoDeployAction,
  verifySecretAction,
} from "./actions";
import {
  adoptHostKeyAction, generateHostKeyAction, installHostKeyAction,
  listDeploymentHostsAction, provisionHostAction,
  registerDeploymentHostAction,
  retireHostAction,
  verifyDeploymentHostAction,
} from "./actions";
import {
  ChangeSetNextStep, plannedDeploymentFrom, type PlannedDeployment,
} from "./deploy-ui";

import { callAction } from "../lib/call-action";
type Secret = {
  secret_ref_id: string; name: string; backend: string; secret_kind: string;
  status: string; fingerprint: string | null; last4: string | null;
  bound_to: string[]; last_verified_at: string | null; created_at: string | null;
};
export type SecretsView = { project_id: string; secrets: Secret[]; error?: string };

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,.09)", borderRadius: 8, padding: 12,
};
const btn: React.CSSProperties = {
  fontSize: 12, padding: "4px 11px", borderRadius: 6, cursor: "pointer",
  border: "1px solid rgba(255,255,255,.18)", background: "transparent", color: "inherit",
};
// One control style for this area, in the console's own font.
//
// Every field was 12px in a hardcoded `monospace` with its label at 11px and 55% opacity, so
// the CI/CD forms read like a terminal dump wedged into the console: labels barely legible,
// mono digits next to proportional prose, and controls smaller than the buttons beside them.
// Mono is kept for the things that ARE literal -- fingerprints, hosts, refs -- and nowhere
// else.
const input: React.CSSProperties = {
  fontSize: 13, padding: "7px 10px", borderRadius: 7,
  background: "var(--surface-2, rgba(255,255,255,.04))",
  border: "1px solid var(--border, rgba(255,255,255,.14))", color: "inherit", minWidth: 150,
};

/**
 * "Still working" — a spinner plus an elapsed-seconds counter.
 *
 * A deploy or teardown is several SSH round trips and can run 10-30 seconds. Silence for that
 * long is indistinguishable from a hang, which is what "the console froze" actually means: not
 * that the browser locked up, but that nothing said whether anything was happening.
 *
 * The seconds matter more than the spinner. A spinner alone proves the animation is running; a
 * rising count proves the REQUEST is still alive, and lets someone judge "slow" against "stuck"
 * without opening devtools.
 *
 * Animated with SVG SMIL rather than CSS keyframes deliberately: this console applies a
 * per-area CSS allow-list, so a new keyframe would need registering in three places and would
 * silently do nothing until it was. Self-contained markup cannot be defeated that way.
 */
function Working({ label }: { label: string }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <span style={{ display: "inline-flex", gap: 7, alignItems: "center", fontSize: 12 }}
          role="status" aria-live="polite">
      <svg width="13" height="13" viewBox="0 0 40 40" aria-hidden="true">
        <circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" strokeWidth="5"
                opacity="0.18" />
        <path d="M20 4 a16 16 0 0 1 16 16" fill="none" stroke="currentColor" strokeWidth="5"
              strokeLinecap="round">
          <animateTransform attributeName="transform" type="rotate" from="0 20 20"
                            to="360 20 20" dur="0.9s" repeatCount="indefinite" />
        </path>
      </svg>
      <span>{label}… {secs}s</span>
    </span>
  );
}

/**
 * Which required fields are empty. Used INSTEAD of disabling a button.
 *
 * A control disabled for missing input is indistinguishable from a broken one: the click does
 * nothing and says nothing, and the operator is left guessing which box matters. This shipped
 * three times in this console — Destroy, the wizard's Continue, and Create target — and each
 * was reported as "nothing happens". Disabling now means only "a request is in flight".
 */
/** `https://1.2.3.4/` -> `1.2.3.4`. Operators paste URLs; the ssh layer needs a host.
 *
 *  Rejecting the paste would be defensible, but silently passing it through is not: it becomes
 *  `ubuntu@http://1.2.3.4/`, which fails much later with a DNS error that names something the
 *  operator never typed. Normalising at the edge keeps the failure legible.
 */
function hostOnly(value: string): string {
  return (value || "").trim()
    .replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, "")   // scheme
    .replace(/\/+$/, "");                            // trailing slashes
}

function missingFields(required: Record<string, unknown>): string[] {
  return Object.entries(required)
    .filter(([, v]) => v === undefined || v === null || String(v).trim() === "")
    .map(([k]) => k);
}

/**
 * A button that shows its own progress.
 *
 * The spinner used to sit BESIDE the controls, so during a 30-second deploy the only feedback
 * was a 13px element several controls away from the one that was pressed. People look at what
 * they clicked; that is where the state has to be. The label becomes "Destroying… 7s" and the
 * button dims, so "is this working?" is answered without hunting.
 *
 * `busy` disables it too — a second click during a deploy would start a second deploy.
 */
function ActionButton({ busy, label, busyLabel, danger, disabled, onClick, title, style }: {
  busy?: boolean; label: string; busyLabel?: string; danger?: boolean; disabled?: boolean;
  onClick: () => void; title?: string; style?: React.CSSProperties;
}) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!busy) { setSecs(0); return; }
    const t = setInterval(() => setSecs((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [busy]);

  return (
    <button
      title={title}
      disabled={!!busy || !!disabled}
      onClick={onClick}
      style={{
        ...btn,
        ...(danger ? { borderColor: "rgba(248,81,73,.5)", color: "#f85149" } : {}),
        ...(busy ? { opacity: 0.75, cursor: "progress" } : {}),
        ...(disabled ? { opacity: 0.5, cursor: "not-allowed" } : {}),
        display: "inline-flex", alignItems: "center", gap: 6,
        ...(style || {}),
      }}>
      {busy && (
        <svg width="12" height="12" viewBox="0 0 40 40" aria-hidden="true">
          <circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" strokeWidth="5"
                  opacity="0.2" />
          <path d="M20 4 a16 16 0 0 1 16 16" fill="none" stroke="currentColor" strokeWidth="5"
                strokeLinecap="round">
            <animateTransform attributeName="transform" type="rotate" from="0 20 20"
                              to="360 20 20" dur="0.9s" repeatCount="indefinite" />
          </path>
        </svg>
      )}
      {busy ? `${busyLabel || label}… ${secs}s` : label}
    </button>
  );
}

function Field({ label, ...rest }: { label: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label style={{ display: "grid", gap: 3 }}>
      <span className="dm-field-label">{label}</span>
      <input style={input} {...rest} />
    </label>
  );
}

// --------------------------------------------------------------------------- //
// Secrets
// --------------------------------------------------------------------------- //
export function SecretsPanel({ initial }: { initial?: SecretsView | null }) {
  const { busy, run: perform } = useBusyAction();
  const pending = !!busy;
  const [msg, setMsg] = useState<string | null>(null);
  const [publicKey, setPublicKey] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [kind, setKind] = useState("api_token");
  const [backend, setBackend] = useState("platform_secret_store");

  const secrets = initial?.secrets ?? [];

  const upload = () => {
    const gaps = missingFields({ name, value });
    if (gaps.length) { setMsg(`fill in: ${gaps.join(", ")}`); return; }
    setMsg(null); setPublicKey(null);
    void perform("Uploading", async () => {
    const out: any = await callAction(putSecretAction)({ name, value, secret_kind: kind, backend });
    // The value is cleared immediately: it has been written through, and keeping it in a form
    // field is keeping a secret in the browser for no reason.
    setValue("");
    setMsg(out?.error ? `failed: ${out.error}` : `stored ${out.name} (${out.fingerprint || out.last4 || "recorded"})`);
    }); };

  const keygen = () => {
    const gaps = missingFields({ name });
    if (gaps.length) { setMsg(`fill in: ${gaps.join(", ")}`); return; }
    setMsg(null); setPublicKey(null);
    void perform("Generating", async () => {
      const out: any = await callAction(generateKeypairAction)({ name, backend });
      if (out?.error) { setMsg(`failed: ${out.error}`); return; }
      setPublicKey(out.public_key);
      setMsg(`generated ${out.fingerprint} — install the PUBLIC key below on the host`);
    });
  };

  return (
    <section className="ct-block">
      <div className="ct-block-title">Secrets</div>
      <div className="detail" style={{ marginBottom: 10 }}>
        BOSS stores a <em>reference</em>, never the value. Nothing here can show you a secret —
        the only key material ever displayed is a public half.
      </div>

      <div style={{ ...box, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="name" placeholder="ssh_key" value={name}
               onChange={(e) => setName(e.target.value)} />
        <label style={{ display: "grid", gap: 3 }}>
          <span className="dm-field-label">kind</span>
          <select style={input} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="api_token">api_token</option>
            <option value="ssh_private_key">ssh_private_key</option>
            <option value="kubeconfig">kubeconfig</option>
            <option value="access_key">access_key</option>
          </select>
        </label>
        <label style={{ display: "grid", gap: 3 }}>
          <span className="dm-field-label">backend</span>
          <select style={input} value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="platform_secret_store">platform_secret_store</option>
            <option value="file">file</option>
            <option value="env">env (read-only)</option>
          </select>
        </label>
        <Field label="value (write-only)" type="password" value={value}
               placeholder="paste — never shown again"
               onChange={(e) => setValue(e.target.value)} />
        <ActionButton label="Upload" busyLabel="Uploading" busy={busy === "Uploading"}
                      onClick={upload} />
        <ActionButton label="Generate keypair" busyLabel="Generating"
                      busy={busy === "Generating"} onClick={keygen}
                      title="BOSS generates the pair, keeps the private half, and shows you the public one" />
        {msg && <span className="detail" style={{ fontSize: 12 }}>{msg}</span>}
      </div>

      {publicKey && (
        <div style={{ ...box, marginTop: 10 }}>
          <div style={{ fontSize: 12, marginBottom: 6 }}>
            Public key — install this on the host. The private half is in the backend and BOSS
            will not show it to anyone, including you.
          </div>
          <code style={{ display: "block", fontSize: 11, wordBreak: "break-all",
                         background: "rgba(255,255,255,.04)", padding: 8, borderRadius: 6 }}>
            {publicKey}
          </code>
          <div className="detail" style={{ fontSize: 11, marginTop: 6 }}>
            <code>{`echo '${publicKey}' >> ~/.ssh/authorized_keys`}</code>
          </div>
        </div>
      )}

      <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 12 }}>
        <thead><tr style={{ textAlign: "left", opacity: .7 }}>
          <th>name</th><th>kind</th><th>backend</th><th>fingerprint</th><th>bound to</th>
          <th>status</th><th></th>
        </tr></thead>
        <tbody>
          {secrets.length === 0 && (
            <tr><td colSpan={7} className="detail">No secrets registered yet.</td></tr>
          )}
          {secrets.map((s) => (
            <tr key={s.secret_ref_id} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
              <td style={{ fontFamily: "monospace" }}>{s.name.split("/").pop()}</td>
              <td>{s.secret_kind}</td>
              <td className="detail">{s.backend}</td>
              <td className="detail" style={{ fontFamily: "monospace", fontSize: 11 }}>
                {s.fingerprint ? s.fingerprint.slice(0, 22) + "…" : (s.last4 ? `…${s.last4}` : "—")}
              </td>
              <td className="detail">{s.bound_to.length ? s.bound_to.join(", ") : "—"}</td>
              <td style={{ color: s.status === "active" ? "#3fb950" : "#d29922" }}>{s.status}</td>
              <td>
                <button style={btn} disabled={pending}
                        onClick={() => void perform("Verifying", async () => {
                          const out: any = await callAction(verifySecretAction)(s.secret_ref_id);
                          setMsg(out?.resolvable
                            ? `${s.name}: resolves ✓${out.fingerprint_unchanged === false ? " (FINGERPRINT CHANGED)" : ""}`
                            : `${s.name}: ${out?.error || "cannot resolve"}`);
                        })}>
                  Verify
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Monitoring — the observability stack and a way into each app's dashboard
// --------------------------------------------------------------------------- //
type ObsApp = {
  app: string; environment: string; port: number; dashboard_url: string | null;
  scrape_url: string;
};
export type Observability = {
  deployed: boolean; grafana_url: string; apps: ObsApp[];
  status: { running: boolean | null; containers?: { name: string; status: string }[];
            error?: string };
  error?: string;
};

/** Stack has never been deployed and there is nothing to scrape — pure empty, not "down". */
export function isObservabilityUnconfigured(data: Observability | null | undefined): boolean {
  if (!data || data.error) return false;
  return !data.deployed && (data.apps?.length ?? 0) === 0;
}

export function MonitoringPanel({ initial, hostTargetId, compact = false }: {
  initial?: Observability | null; hostTargetId?: string;
  /** Rendered inside the health panel as its footer: the stack's own machinery only, without
   *  the app table — those rows now sit on the health rows they describe, so repeating them
   *  here is the duplication this merge removed. */
  compact?: boolean;
}) {
  const { busy, run: perform } = useBusyAction();
  const [msg, setMsg] = useState<string | null>(null);
  const data = initial;

  if (!data || data.error) {
    return (
      <section className="ct-block">
        <div className="ct-block-title">Observability stack</div>
        <div className="detail" style={{ color: data?.error ? "#f85149" : undefined }}>
          {data?.error ? `Monitoring: ${data.error}` : "Loading…"}
        </div>
      </section>
    );
  }

  // Not deployed + no apps is setup debt, not a stack outage. Do not render "● stack not
  // running", empty scrape tables, or a Deploy button that can only fail for lack of a host.
  if (isObservabilityUnconfigured(data)) {
    return (
      <section className="ct-block" data-empty-state="stack-not-deployed">
        <div className="ct-block-title">Observability stack</div>
        <p className="empty-state" style={{ padding: "18px 0" }}>
          The observability stack is not deployed. There is no Prometheus, Grafana, or
          scrape config for this project yet.
        </p>
        <p className="detail" style={{ marginTop: 0 }}>
          Onboard a deployment target in CI/CD first — the stack borrows that host and
          credential. After a target exists, return here to deploy the collector /
          Prometheus / Grafana stack.
        </p>
        {!hostTargetId && (
          <p className="detail" data-testid="obs-needs-target" style={{ color: "#d29922" }}>
            No active deployment target is available to host the stack.
          </p>
        )}
      </section>
    );
  }

  const running = data.status?.running;
  // A footer inside the health panel, not a panel of its own: a nested `ct-block` would draw a
  // second bordered card and undo the merge it exists to complete.
  const Frame = compact
    ? ({ children }: { children: React.ReactNode }) =>
        <div data-empty-state={data.deployed ? "stack-deployed" : "stack-pending"}>{children}</div>
    : ({ children }: { children: React.ReactNode }) =>
        <section className="ct-block"
                 data-empty-state={data.deployed ? "stack-deployed" : "stack-pending"}>
          {children}
        </section>;
  return (
    <Frame>
      <div className={compact ? "dh-stack-title" : "ct-block-title"}>
        Observability stack{" "}
        <span style={{ fontSize: 12,
                       color: running ? "#3fb950" : running === false ? "#d29922" : "#8b949e" }}>
          ● {data.deployed
            ? (running ? "stack running" : running === false ? "stack not running" : "deployed")
            : "not deployed"}
        </span>
      </div>

      <div className="detail" style={{ marginBottom: 10 }}>
        One OpenTelemetry collector, one Prometheus and one Grafana for every app.
        {" "}The scrape config is generated from the deployment targets {compact ? "above" : "below"} —
        onboarding an app is what puts it on a dashboard, so the two cannot drift.
      </div>

      <div style={{ ...box, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <ActionButton busy={!!busy}
                label={data.deployed ? "Redeploy stack" : "Deploy monitoring stack"}
                busyLabel="Deploying stack"
                title="Regenerate the config from the registry and deploy"
                onClick={() => {
                  if (!hostTargetId) {
                    setMsg("no active deployment target — the stack borrows a host and "
                           + "credential from one, so onboard an app first");
                    return;
                  }
                  setMsg(null);
                  void perform("Deploying stack", async () => {
                  const out: any = await callAction(deployObservabilityAction)(hostTargetId!);
                  setMsg(out?.error
                    ? `failed: ${typeof out.error === "string" ? out.error : JSON.stringify(out.error).slice(0, 200)}`
                    : `stack deployed — ${out.apps?.length ?? 0} app(s) on ${out.grafana_url}`);
                  }); }} />
        {data.deployed && data.grafana_url && (
          <a href={data.grafana_url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            Open Grafana ↗
          </a>
        )}
        {busy && <Working label={busy} />}
        {msg && <span className="detail" style={{ fontSize: 12 }}>{msg}</span>}
      </div>

      {/* The app table lives on the HEALTH rows now — same apps, same environments, and the
          question "is it up and where do I look at it" is one question. Kept for the standalone
          panel, which still has to stand alone. */}
      {compact ? (
        data.apps.length === 0 ? (
          <div className="detail" style={{ marginTop: 8 }}>
            No active deployment targets, so there is nothing to scrape yet.
          </div>
        ) : null
      ) : (
      <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 12 }}>
        <thead><tr style={{ textAlign: "left", opacity: .7 }}>
          <th>app</th><th>environment</th><th>scraped at</th><th>dashboard</th>
        </tr></thead>
        <tbody>
          {data.apps.length === 0 && (
            <tr><td colSpan={4} className="detail">
              No active deployment targets, so there is nothing to scrape yet.
            </td></tr>
          )}
          {data.apps.map((a) => (
            <tr key={a.app} style={{ borderTop: "1px solid rgba(255,255,255,.07)" }}>
              <td>{a.app}</td>
              <td><span className="cap-badge tone-blue">{a.environment}</span></td>
              <td className="detail" style={{ fontFamily: "monospace" }}>
                {a.scrape_url}/metrics
              </td>
              <td>
                {a.dashboard_url
                  ? <a href={a.dashboard_url} target="_blank" rel="noreferrer">Open dashboard ↗</a>
                  : <span className="detail">deploy the stack first</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}

      {data.status?.containers && data.status.containers.length > 0 && (
        <div className="detail" style={{ fontSize: 11, marginTop: 8 }}>
          {data.status.containers.map((c) => `${c.name}: ${c.status}`).join(" · ")}
        </div>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------- //
// Onboarding wizard
// --------------------------------------------------------------------------- //
export function OnboardWizard({ secrets }: { secrets: Secret[] }) {
  const [open, setOpen] = useState(false);
  const { busy, run: perform } = useBusyAction();
  const [step, setStep] = useState(1);
  const [log, setLog] = useState<string[]>([]);
  // What BOSS is configured to clone. Shown rather than assumed: the form used to take a bare
  // repo NAME and silently prepend an owner from config, so nothing on screen said which
  // GitHub repository would actually be fetched.
  const [defaults, setDefaults] = useState<{ owner?: string; repo?: string; repo_url?: string }>({});
  const [repos, setRepos] = useState<{ full_name: string; url: string; code_tasks: number }[]>([]);
  // Apps the chosen repo actually contains. BOSS computes this list anyway when resolving a
  // deploy — showing it up front turns a guess into a choice, instead of failing a minute later
  // with a wall of candidates.
  const [apps, setApps] = useState<{ entry: string; kind: string; label: string }[]>([]);
  const [appsAmbiguous, setAppsAmbiguous] = useState(false);
  useEffect(() => {
    let live = true;
    callAction(getDeliveryDefaultsAction)().then((d) => {
      if (!live || !d || d.error) return;
      setDefaults(d);
      // Set the VALUE, not a placeholder. Showing the default repo as placeholder text made an
      // empty field look filled — an operator typed nothing because it already read
      // "boss-sandbox", then got "fill in: repo" for a field that appeared complete.
      setForm((f) => (f.repo ? f : { ...f, repo: d.repo || "" }));
    });
    // BOSS already knows every repo it has worked in — asking an operator to type one from
    // memory is both unnecessary and a chance to typo a name that fails later at clone time.
    callAction(listReposAction)().then((d) => { if (live && d?.repos) setRepos(d.repos); });
    return () => { live = false; };
  }, []);
  const [targetId, setTargetId] = useState<string | null>(null);

  const [form, setForm] = useState({
    app_name: "", repo: "", environment: "staging", target_kind: "ssh",
    target_ref: "", domain: "", scheme: "http", app_port: "", app_entry: "",
    publishable_ports: "",
    secret_ref_id: "",
  });
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }));
  const say = (line: string) => setLog((l) => [...l, line]);

  const create = () => { void perform("Creating target", async () => {
    // Normalise before validating, so a pasted URL is accepted rather than rejected for a
    // reason the operator cannot see.
    const user = form.target_ref.includes("@") ? form.target_ref.split("@")[0] : "";
    const hostPart = hostOnly(form.target_ref.includes("@")
      ? form.target_ref.slice(form.target_ref.indexOf("@") + 1) : form.target_ref);
    const targetRef = user ? `${user}@${hostPart}` : hostPart;
    const domain = hostOnly(form.domain);
    if (targetRef !== form.target_ref || domain !== form.domain) {
      setForm((f) => ({ ...f, target_ref: targetRef, domain }));
      say(`normalised: host=${targetRef}${domain ? `, domain=${domain}` : ""}`);
    }

    const gaps = missingFields({
      "app name": form.app_name, repo: form.repo, "host (user@host)": targetRef,
    });
    if (gaps.length) { say(`fill in: ${gaps.join(", ")}`); return; }
    if (!targetRef.includes("@")) {
      say("host must include the ssh user, e.g. ubuntu@198.51.100.139");
      return;
    }
    if (appsAmbiguous && !form.app_entry) {
      say(`this repo contains ${apps.length} runnable apps — choose which one this target is, `
          + "or the deploy cannot tell them apart");
      return;
    }
    say("registering target…");
    const out = await callAction(createTargetAction)({
      app_name: form.app_name, repo: form.repo, environment: form.environment,
      target_kind: form.target_kind, target_ref: targetRef,
      domains: domain ? [domain] : [],
      app_port: form.app_port ? Number(form.app_port) : undefined,
      app_entry: form.app_entry || undefined,
      publishable_ports: form.publishable_ports || undefined,
      health_checks: [{ kind: "http", path: "/", expect: 200, scheme: form.scheme }],
    });
    if (out?.error) { say(`failed: ${out.error}`); return; }
    setTargetId(out.id);
    say(`target ${out.id} created`);
    if (form.secret_ref_id) {
      const bound = await callAction(bindSecretAction)(out.id, "ssh_key", form.secret_ref_id);
      say(bound?.error ? `binding failed: ${bound.error}` : "credential bound");
    }
    setStep(2);
  }); };

  if (!open) {
    return (
      <button style={{ ...btn, padding: "6px 14px" }} onClick={() => setOpen(true)}>
        + Register deployment target
      </button>
    );
  }

  return (
    <section className="ct-block">
      <div className="ct-block-title">
        Register deployment target{" "}
        <span className="detail" style={{ fontSize: 12 }}>step {step} of 2</span>
      </div>

      {step === 1 && (
        <div style={{ display: "grid", gap: 10 }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <Field label="app name" value={form.app_name}
                   onChange={(e) => set("app_name", e.target.value)} placeholder="stock-app" />
            <label style={{ display: "grid", gap: 3 }}>
              <span className="dm-field-label">repo</span>
              <input style={input} list="boss-known-repos" value={form.repo}
                     placeholder="owner/name"
                     onChange={(e) => set("repo", e.target.value)} />
              {/* A datalist rather than a select: it offers what BOSS knows while still
                  accepting a repo it has never seen, which is the whole point of onboarding. */}
              <datalist id="boss-known-repos">
                {repos.map((r) => <option key={r.full_name} value={r.full_name} />)}
              </datalist>
            </label>
            <label style={{ display: "grid", gap: 3 }}>
              <span className="dm-field-label">environment</span>
              <select style={input} value={form.environment}
                      onChange={(e) => set("environment", e.target.value)}>
                <option value="staging">staging</option>
                <option value="dev">dev</option>
                <option value="preview">preview</option>
                <option value="production">production (human-gated)</option>
              </select>
            </label>
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <label style={{ display: "grid", gap: 3 }}>
              <span className="dm-field-label">destination</span>
              <select style={input} value={form.target_kind}
                      onChange={(e) => set("target_kind", e.target.value)}>
                <option value="ssh">ssh — a Linux host running docker</option>
                <option value="container">container</option>
              </select>
            </label>
            <Field label="host (user@host)" value={form.target_ref}
                   onChange={(e) => set("target_ref", e.target.value)}
                   placeholder="ubuntu@203.0.113.10" />
            <Field label="domain / ip:port" value={form.domain}
                   onChange={(e) => set("domain", e.target.value)}
                   placeholder="203.0.113.10:3010" />
            <label style={{ display: "grid", gap: 3 }}>
              <span className="dm-field-label">health scheme</span>
              <select style={input} value={form.scheme}
                      onChange={(e) => set("scheme", e.target.value)}>
                <option value="http">http</option>
                <option value="https">https</option>
              </select>
            </label>
            <Field label="port (required for deployment)" value={form.app_port}
                   onChange={(e) => set("app_port", e.target.value)} placeholder="3010" />
            {/* A declared app port still needs to be reachable through the host firewall. */}
            <Field label="ports this host publishes (optional)"
                   title="Comma-separated, ranges allowed — e.g. 3003,4000,10000-10001."
                   value={form.publishable_ports}
                   onChange={(e) => set("publishable_ports", e.target.value)}
                   placeholder="3003,4000,10000-10001" />
            <label style={{ display: "grid", gap: 3 }}>
              <span className="dm-field-label">
                app entry {appsAmbiguous ? "(required — this repo has several)" : "(optional)"}
              </span>
              <select style={input} value={form.app_entry}
                      onFocus={() => {
                        if (apps.length || !form.repo) return;
                        const [o, n] = form.repo.includes("/")
                          ? form.repo.split("/") : [defaults.owner || "", form.repo];
                        callAction(listRepoAppsAction)(o, n).then((d) => {
                          if (d?.apps) { setApps(d.apps); setAppsAmbiguous(!!d.ambiguous); }
                        });
                      }}
                      onChange={(e) => set("app_entry", e.target.value)}>
                <option value="">
                  {apps.length ? "— choose which app this target is —" : "— click to load apps —"}
                </option>
                {apps.map((a) => (
                  <option key={a.entry} value={a.entry}>{a.label}</option>
                ))}
              </select>
            </label>
          </div>
          <label style={{ display: "grid", gap: 3 }}>
            <span className="dm-field-label">
              credential (ssh_key)
            </span>
            <select style={input} value={form.secret_ref_id}
                    onChange={(e) => set("secret_ref_id", e.target.value)}>
              <option value="">— none: fall back to the global key —</option>
              {secrets.filter((s) => s.secret_kind.includes("private_key")).map((s) => (
                <option key={s.secret_ref_id} value={s.secret_ref_id}>
                  {s.name.split("/").pop()} {s.fingerprint ? `(${s.fingerprint.slice(0, 16)}…)` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="detail" style={{ fontSize: 12 }}>
            BOSS will clone{" "}
            <code>
              {form.repo
                ? (form.repo.includes("/")
                    ? `https://github.com/${form.repo}`
                    : `https://github.com/${defaults.owner || "?"}/${form.repo}`)
                : (defaults.repo_url || "— enter a repo —")}
            </code>
            {!form.repo?.includes("/") && defaults.owner &&
              ` (owner ${defaults.owner} comes from BOSS config; paste owner/name to override)`}
          </div>
          <div className="detail" style={{ fontSize: 12 }}>
            <strong>app entry</strong> is the file or directory that SERVES this app —
            <code>serve_nqueens.py</code>, not the <code>nqueens_core</code> package it imports.
            Click the field to load the apps BOSS finds in this repo. A single-app repo can
            leave it blank.
          </div>
          <div className="detail" style={{ fontSize: 12 }}>
            No key yet? Generate one in <strong>Secrets</strong> above, install the public half
            on the host, then come back — that install is the one step BOSS cannot do for you,
            because anything that could would already need access to the host.
          </div>
          <div>
            <ActionButton label="Create target →" busyLabel="Creating target"
                          busy={busy === "Creating target"} onClick={create} />
            <button style={{ ...btn, marginLeft: 8 }} onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div style={{ display: "grid", gap: 10 }}>
          <div className="detail">
            Target <code>{targetId}</code> saved. Host preparation is paused until BOSS can approve the change,
            record what happened, and provide recovery steps. Set the app port on the target;
            BOSS will not pick one on the host automatically.
          </div>
          <div>
            <button style={{ ...btn, opacity: 0.5, cursor: "not-allowed" }} disabled
                    title="Host preparation awaits approval and recovery steps">
              Prepare host unavailable
            </button>
            <button style={{ ...btn, marginLeft: 8 }} onClick={() => setOpen(false)}>Done</button>
          </div>
        </div>
      )}

    </section>
  );
}

// --------------------------------------------------------------------------- //
// Per-target trigger controls
// --------------------------------------------------------------------------- //
/**
 * Run one action, and tell the truth about it while it runs.
 *
 * Two obligations that were being met separately and therefore inconsistently:
 *
 *   1. FEEDBACK STARTS ON THE CLICK. These handlers used `useTransition`, and a state update
 *      made inside a transition is low priority. The busy flag is set outside a transition,
 *      so the first paint after a click shows progress.
 *
 *   2. THE PAGE REFRESHES AFTER. Every panel here is server-rendered from the API;
 *      `router.refresh()` re-runs the server component with fresh data.
 */
function useBusyAction(onChanged?: () => void | Promise<void>) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (label: string, fn: () => Promise<unknown>): Promise<any> => {
    setBusy(label);
    try {
      return await fn();
    } finally {
      setBusy(null);
      // Both, and neither is redundant. `router.refresh()` updates every OTHER panel fed by
      // the server render; `onChanged` re-fetches the one panel that owns this row, because
      // the server refresh alone left a deleted target on screen until a manual reload.
      router.refresh();
      await onChanged?.();
    }
  };
  return { busy, run };
}

function TeardownControls() {
  return <span className="detail" style={{ fontSize: 12 }}
               title="Host removal awaits approval and recovery steps">
    Host removal unavailable until the approval flow is ready
  </span>;
}

export function TargetControls({ target, shipped, onChanged, communityEdition = true }: {
  shipped?: boolean;
  onChanged?: () => void | Promise<void>;
  communityEdition?: boolean;
  target: { id: string; app_name: string; status?: string; repo?: string;
            repo_owner?: string | null; auto_deploy?: boolean; paused?: boolean };
}) {
  const [state, setState] = useState({ auto: !!target.auto_deploy, paused: !!target.paused });
  const [msg, setMsg] = useState<string | null>(null);
  // Feedback starts on the click and the page refreshes after — see useBusyAction.
  const { busy, run: perform } = useBusyAction(onChanged);
  const pending = !!busy;
  const [ref, setRef] = useState("");
  const [plan, setPlan] = useState<PlannedDeployment | null>(null);
  // Branches for THIS target's repo, so "which build" is a choice rather than a memory test.
  // Loaded lazily: a page with several targets should not fan out to the repo host on render.
  const [refs, setRefs] = useState<{ name: string; sha: string }[]>([]);
  const [refsLoaded, setRefsLoaded] = useState(false);
  const loadRefs = async () => {
    if (refsLoaded || !target.repo) return;
    setRefsLoaded(true);
    // The owner comes from the repo string, else the target, else the project default. Asking
    // GitHub for `//boss-sandbox/refs` returns a 404 and an empty list, which looks exactly
    // like a repo with no branches — the picker fails silently, which is the worst way to fail.
    let owner = target.repo.includes("/") ? target.repo.split("/")[0] : (target.repo_owner || "");
    const name = target.repo.includes("/")
      ? target.repo.split("/").slice(1).join("/") : target.repo;
    if (!owner) owner = (await callAction(getDeliveryDefaultsAction)())?.owner || "";
    if (!owner) return;
    const d = await callAction(listRefsAction)(owner, name);
    if (d?.branches) setRefs(d.branches);
    if (!ref && d?.default_branch) setRef(d.default_branch);
  };

  // Retired targets keep their configuration and deployment history. Direct host setup and
  // history deletion are paused until their approved, recoverable workflows are available.
  if (target.status && target.status !== "active") {
    return (
      <span style={{ display: "inline-flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span className="detail" style={{ fontSize: 12 }}>
          {target.status} — its configuration is kept
        </span>
        <span className="detail" style={{ fontSize: 12 }}>
          Restore and permanent deletion are paused until approval and recovery steps are ready.
        </span>
      </span>
    );
  }

  const toggle = (patch: { auto_deploy?: boolean; paused?: boolean }) =>
    void perform("Saving", async () => {
      const out: any = await callAction(setAutoDeployAction)(target.id, patch);
      if (out?.error) { setMsg(out.error); return; }
      setState({ auto: !!out.auto_deploy, paused: !!out.paused });
      setMsg(null);
    });

  return (
   <div style={{ display: "grid", gap: 8 }}>
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
      <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={state.auto} disabled={pending || communityEdition}
               onChange={(e) => toggle({ auto_deploy: e.target.checked })} />
        {communityEdition ? "plan on merge (paused in Community)" : "plan deployment on merge"}
      </label>
      <label style={{ display: "flex", gap: 6, alignItems: "center",
                      color: state.paused ? "#d29922" : undefined }}>
        <input type="checkbox" checked={state.paused} disabled={pending}
               onChange={(e) => toggle({ paused: e.target.checked })} />
        paused
      </label>
      <input style={{ ...input, minWidth: 150 }} value={ref} list={`refs-${target.id}`}
             placeholder="branch or sha (default: main)" onFocus={loadRefs}
             onChange={(e) => setRef(e.target.value)} />
      <datalist id={`refs-${target.id}`}>
        {refs.map((r) => <option key={r.name} value={r.name}>{r.sha}</option>)}
      </datalist>
      {/* A request creates a reviewable ChangeSet; it does not start execution. */}
      <ActionButton label={shipped ? "Plan redeployment" : "Plan deployment"}
              busyLabel="Planning"
              disabled={communityEdition || !!plan}
              title={communityEdition
                ? "Community host deployment awaits approval and recovery steps"
                : plan
                  ? "This deployment ChangeSet is awaiting review"
                : shipped
                  ? "Create a new ChangeSet to review before redeploying this target"
                  : "Create a ChangeSet to review before deploying this target"}
              busy={busy === "Planning"}
              onClick={() => { setMsg(null); setPlan(null);
                void perform("Planning", async () => {
                  const out: any = await callAction(deployTargetAction)(target.id, ref);
                  if (out?.error) {
                    setMsg(String(out.error));
                    return;
                  }
                  const nextPlan = plannedDeploymentFrom(out);
                  if (!nextPlan) {
                    setMsg("Deployment request did not return a reviewable ChangeSet. No deployment was started.");
                    return;
                  }
                  setPlan(nextPlan);
                }); }} />
      <TeardownControls />
      {communityEdition && <span className="detail">
        Community deployment is paused until approval and recovery steps are ready.
      </span>}
      {msg && <span className="detail" role="alert" style={{ color: "#f85149" }}>{msg}</span>}
    </div>
    {plan && (
      <div style={{ maxWidth: 760 }}>
        <ChangeSetNextStep plan={plan} />
      </div>
    )}
   </div>
  );
}

/**
 * The fleet: VMs, and the apps riding on each — as a WIZARD, not a control panel.
 *
 * The first version put every capability in one row: adopt, generate, install, provision,
 * verify, and a backend picker. Six same-weight buttons with no order, so the honest question
 * ("what do I press next?") had no answer on screen — and the wrong answer (Generate key)
 * silently ROTATED the credential and invalidated an install command the operator was
 * part-way through running.
 *
 * So the stage is DERIVED from the host's own state rather than chosen by the operator, and
 * only the action that advances that stage is offered as primary. Everything else stays
 * reachable but demoted. The rule this encodes: a wizard that lets you press step 4 while
 * step 2 is unfinished is a control panel wearing a costume.
 */

type FleetHost = {
  id: string; name: string; address: string; ssh_user: string; ssh_port: number;
  status: string; arch?: string | null; app_count: number;
  apps?: Array<{ app_name: string }>;
  key_secret_ref?: string | null;
  install_command?: string | null;
  install_variants?: Record<string, string | boolean> | null;
  last_preflight?: {
    reachable?: boolean; problems?: string[];
    findings?: Array<{ kind: string; detail: string; remedy: string }>;
  } | null;
};

/** A global setting that stops the whole wizard working, whatever the operator does here. */
type Blocker = { setting: string; message: string };

type Stage = "credential" | "install" | "verify" | "provision" | "ready";

const STAGES: Array<{ id: Stage; label: string }> = [
  { id: "credential", label: "Credential" },
  { id: "install", label: "Authorise" },
  { id: "verify", label: "Verify" },
  { id: "provision", label: "Provision" },
  { id: "ready", label: "Ready" },
];

/** Where this host actually is, read from its own state — never from what was last clicked. */
/** Finding kinds are snake_case identifiers; operators should not have to read them as such. */
export function findingLabel(kind: string): string {
  const named: Record<string, string> = {
    credential_lost: "Key no longer in the store",
    key_not_installed: "Key not on the host yet",
    expired_root_password: "Password expired on the host",
    outward_ssh_disabled: "SSH deploys are switched off",
    docker_missing: "Docker missing",
    other: "Problem",
  };
  return named[kind] ?? kind.replace(/_/g, " ");
}

export function hostStage(h: FleetHost): Stage {
  const pf = h.last_preflight ?? {};
  const problems = pf.problems ?? [];
  if (h.status === "verified") return "ready";
  // A BINDING IS NOT A CREDENTIAL. `key_secret_ref` says a secret was recorded, not that it
  // still resolves — and when the store lost it, the wizard cheerfully showed "Credential ✓"
  // while preflight reported "secret 'ssh_key' is bound but unusable". Trust the last real
  // attempt over the pointer: if resolution failed, we are back at step one.
  const credentialGone = problems.some((p) => /bound but unusable|does not exist/i.test(p));
  if (!h.key_secret_ref || credentialGone) return "credential";
  // A pending install line means BOSS minted a key that is not on the box yet. That is a
  // distinct state from "we never had a credential", and conflating them is what made
  // "Generate key" look like the remedy for a key that was already generated.
  if (h.install_command && !pf.reachable) return "install";
  if (!pf.reachable) return "verify";
  if (problems.some((p) => /docker/i.test(p))) return "provision";
  return "verify";
}

function StageBar({ current }: { current: Stage }) {
  const at = STAGES.findIndex((s) => s.id === current);
  return (
    <ol className="fleet-steps" aria-label="Host onboarding progress">
      {STAGES.map((s, i) => (
        <li key={s.id} className="fleet-step"
            data-state={i < at ? "done" : i === at ? "current" : "todo"}
            aria-current={i === at ? "step" : undefined}>
          <span className="fleet-step-dot">{i < at ? "✓" : i + 1}</span>
          <span>{s.label}</span>
        </li>
      ))}
    </ol>
  );
}

export function FleetPanel({ projectId, targets = [] }: {
  projectId: string;
  /** The project's deployment targets, so a verified host can be linked to an app without
   *  leaving the wizard — "where does this run?" is the question a freshly-onboarded host
   *  immediately raises, and answering it elsewhere is how hosts end up verified and unused. */
  targets?: Array<{ id: string; app_name: string; environment: string; host_id?: string | null }>;
}) {
  const [hosts, setHosts] = React.useState<FleetHost[]>([]);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);
  const [minted, setMinted] = React.useState<Record<string, any>>({});
  const [openReg, setOpenReg] = React.useState(false);
  const [adoptFor, setAdoptFor] = React.useState<string | null>(null);
  const [keyBackend, setKeyBackend] = React.useState("database");
  const [copied, setCopied] = React.useState<string | null>(null);
  const [route, setRoute] = React.useState<Record<string, string>>({});
  const [blocker, setBlocker] = React.useState<Blocker | null>(null);
  const [hostWritesAllowed, setHostWritesAllowed] = React.useState(false);
  const [hostWriteBlocker, setHostWriteBlocker] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    const r = await callAction(listDeploymentHostsAction)(projectId);
    if (r.ok) {
      setHosts((((r.data as any)?.hosts ?? []) as FleetHost[]));
      setBlocker(((r.data as any)?.preconditions?.blocker ?? null) as Blocker | null);
      setHostWritesAllowed((r.data as any)?.preconditions?.host_writes_allowed === true);
      setHostWriteBlocker((r.data as any)?.preconditions?.host_write_blocker?.message ?? null);
    }
    else setErr(r.error ?? "could not load hosts");
  }, [projectId]);

  React.useEffect(() => { void reload(); }, [reload]);

  async function run(fn: () => Promise<{ ok: boolean; error?: string; data?: any }>,
                    after?: (d: any) => void) {
    setBusy(true); setErr(null);
    try {
      const r = await fn();
      if (!r.ok) setErr(r.error ?? "failed");
      else after?.(r.data);
      await reload();
    } finally { setBusy(false); }
  }

  return (
    <section className="ct-block" data-testid="fleet-panel">
      <div className="ct-block-title">Hosts</div>
      <p className="detail" style={{ marginTop: 0 }}>
        The machines apps deploy to. One key per host — rotating it never touches the apps
        riding on it.
      </p>

      {/* Before anything else. This was discoverable only by walking four steps and being
          told at Verify that BOSS was never going to connect — after a key had been minted
          and pasted onto a real machine. */}
      {blocker ? (
        <div className="fleet-blocker" data-testid="fleet-blocker">
          <strong>{blocker.message}</strong>
          <span className="detail"> Setting: <code>{blocker.setting}</code></span>
        </div>
      ) : null}
      {hostWriteBlocker ? (
        <p className="detail" data-testid="fleet-host-write-blocker">{hostWriteBlocker}</p>
      ) : null}

      {err ? <div className="cap-badge tone-red" data-testid="fleet-error">{err}</div> : null}

      {hosts.length === 0 ? (
        <p className="detail">No hosts yet. Register one to give BOSS somewhere to deploy.</p>
      ) : null}

      <div style={{ display: "grid", gap: 14 }}>
        {hosts.map((h) => {
          const stage = hostStage(h);
          const pf = h.last_preflight ?? {};
          const cmd = minted[h.id]?.install_command || h.install_command;
          const tone = h.status === "verified" ? "tone-green"
            : h.status === "degraded" ? "tone-red" : "tone-amber";
          return (
            <div key={h.id} className="ct-outcome" data-testid="fleet-host"
                 data-stage={stage} style={{ display: "grid", gap: 8 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong>{h.name}</strong>
                <code className="detail">{h.ssh_user}@{h.address}:{h.ssh_port}</code>
                <span className={`cap-badge ${tone}`}>{h.status}</span>
                {h.arch ? <span className="cap-badge tone-blue">{h.arch}</span> : null}
                <span className="detail">
                  {h.app_count} app{h.app_count === 1 ? "" : "s"}
                  {h.app_count ? `: ${(h.apps ?? []).map((a) => a.app_name).join(", ")}` : ""}
                </span>
              </div>

              <StageBar current={stage} />

              {/* ── STEP 1: a credential. Two honest routes, stated as a choice. ── */}
              {stage === "credential" ? (
                <div className="fleet-stage">
                  <p className="detail" style={{ margin: 0 }}>
                    BOSS needs a way in. It cannot place the FIRST key on a machine it has no
                    access to — so either lend it a key that already works, or have it mint one
                    for you to push.
                  </p>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <ActionButton busy={busy}
                      label={adoptFor === h.id ? "Cancel" : "I have a key that works"}
                      busyLabel="…" onClick={() => setAdoptFor(adoptFor === h.id ? null : h.id)} />
                    <ActionButton busy={busy} label="Generate a key for me" busyLabel="Generating…"
                      title="BOSS mints a keypair; you push the public half with one command"
                      onClick={() => run(
                        () => callAction(generateHostKeyAction)(null, formOf({ host_id: h.id, backend: keyBackend })),
                        (d) => setMinted((p) => ({ ...p, [h.id]: d })))} />
                    <label className="detail" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                      store in
                      <select value={keyBackend} onChange={(e) => setKeyBackend(e.target.value)}
                              aria-label="Secret backend for this host's key">
                        {/* Durable by default. `file` writes to the container's own disk,
                            so a deploy silently destroys every key BOSS generated. */}
                        <option value="database">database (survives restarts)</option>
                        <option value="file">file (local disk — lost on redeploy)</option>
                        <option value="platform_secret_store">platform_secret_store</option>
                      </select>
                    </label>
                  </div>

                  {adoptFor === h.id ? (
                    <form style={{ display: "grid", gap: 6, marginTop: 6 }}
                      onSubmit={(e) => {
                        e.preventDefault();
                        const fd = new FormData(e.currentTarget as HTMLFormElement);
                        fd.set("host_id", h.id); fd.set("backend", keyBackend);
                        void run(() => callAction(adoptHostKeyAction)(null, fd), () => setAdoptFor(null));
                      }}>
                      <span className="detail">
                        Paste the PRIVATE key that already works on {h.address}. It goes straight
                        to the secret store — BOSS keeps a reference and a fingerprint, never the
                        key itself.
                      </span>
                      <textarea name="private_key" rows={4} required
                                aria-label="Existing private key for this host"
                                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                                style={{ width: "100%", fontFamily: "var(--mono)", fontSize: 12 }} />
                      <div><button className="cap-btn" type="submit" disabled={busy}>Adopt key</button></div>
                    </form>
                  ) : null}
                </div>
              ) : null}

              {/* ── STEP 2: the one thing BOSS cannot do for you. ── */}
              {stage === "install" && cmd ? (
                <div className="fleet-stage" data-testid="fleet-install">
                  {/* Onboarding does not fail because a customer is careless — it fails because
                      the tool assumed a situation they are not in. So ask which one. */}
                  <label className="detail" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    How will you reach {h.address}?
                    <select value={route[h.id] ?? "from_laptop"}
                            aria-label="How you can reach this host"
                            onChange={(e) => setRoute((p) => ({ ...p, [h.id]: e.target.value }))}>
                      <option value="from_laptop">I can SSH to it from my machine</option>
                      <option value="on_host">Only a provider web console</option>
                      <option value="cloud_init">It doesn’t exist yet</option>
                    </select>
                  </label>
                  <strong style={{ color: "var(--amber, #F5B544)" }}>
                    {(route[h.id] ?? "from_laptop") === "from_laptop"
                      ? `Run this FROM YOUR MACHINE — it pushes the key to ${h.ssh_user}@${h.address}.`
                      : (route[h.id] === "on_host"
                        ? "Paste this INTO THE PROVIDER CONSOLE, logged in on the host."
                        : "Give this as user-data when you create the machine — then nothing manual happens at all.")}
                  </strong>
                  <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", margin: "6px 0" }}>
                    {(() => {
                      const v = (minted[h.id]?.install_variants || h.install_variants) as any;
                      const key = route[h.id] ?? "from_laptop";
                      return (v?.[key]) || cmd;
                    })()}
                  </pre>
                  {(route[h.id] ?? "from_laptop") === "from_laptop" ? (
                    <p className="detail" style={{ margin: 0 }}>
                      <code>Permission denied (publickey)</code> here means <em>your</em> access
                      needs a key — set “Your SSH key” on the host and this command will carry
                      the right <code>-i</code>.
                    </p>
                  ) : null}
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <ActionButton busy={false} label={copied === h.id ? "Copied" : "Copy command"}
                      busyLabel="…" onClick={() => {
                        void navigator.clipboard?.writeText(cmd);
                        setCopied(h.id); window.setTimeout(() => setCopied(null), 2000);
                      }} />
                    <ActionButton busy={busy} label="I've run it — verify" busyLabel="Connecting…"
                      onClick={() => run(() => callAction(verifyDeploymentHostAction)(null, formOf({ host_id: h.id })))} />
                  </div>
                  {/* The trap this wizard exists to close. */}
                  <p className="detail" style={{ margin: "6px 0 0" }}>
                    Expect <code>BOSS_KEY_INSTALLED</code>. Don’t generate another key — that
                    mints a new one and invalidates this command.
                  </p>
                </div>
              ) : null}

              {/* ── STEP 3: prove it, with a real session. ── */}
              {stage === "verify" ? (
                <div className="fleet-stage">
                  <p className="detail" style={{ margin: 0 }}>
                    BOSS has a credential for this host. Verify opens a real SSH session — it is
                    evidence, not a checkbox.
                  </p>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <ActionButton busy={busy} label="Verify" busyLabel="Connecting…"
                      onClick={() => run(() => callAction(verifyDeploymentHostAction)(null, formOf({ host_id: h.id })))} />
                    {hostWritesAllowed ? (
                      <ActionButton busy={busy} label="Let BOSS install its own key"
                        busyLabel="Installing…"
                        title="Replaces the borrowed credential with one BOSS owns and can rotate"
                        onClick={() => run(() => callAction(installHostKeyAction)(
                          null, formOf({ host_id: h.id, backend: keyBackend })))} />
                    ) : null}
                  </div>
                </div>
              ) : null}

              {/* ── STEP 4: make it able to actually run something. ── */}
              {stage === "provision" ? (
                <div className="fleet-stage">
                  <p className="detail" style={{ margin: 0 }}>
                    Reachable, but it cannot run a deployment yet — BOSS ships apps as
                    containers and this host has no docker.
                  </p>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {hostWritesAllowed ? (
                      <ActionButton busy={busy} label="Install docker" busyLabel="Provisioning…"
                        onClick={() => run(() => callAction(provisionHostAction)(null, formOf({ host_id: h.id })))} />
                    ) : null}
                    <ActionButton busy={busy} label="Verify" busyLabel="Connecting…"
                      onClick={() => run(() => callAction(verifyDeploymentHostAction)(null, formOf({ host_id: h.id })))} />
                  </div>
                </div>
              ) : null}

              {/* ── DONE. ── */}
              {stage === "ready" ? (
                <div className="fleet-stage" data-testid="fleet-ready">
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="cap-badge tone-green">Ready to deploy</span>
                    {h.app_count ? (
                      <span className="detail">
                        Running here: {(h.apps ?? []).map((a) => a.app_name).join(", ")}
                      </span>
                    ) : (
                      <span className="detail">Nothing points here yet.</span>
                    )}
                  </div>

                  {/* WHICH APPS RUN HERE — and nothing about CHOOSING them.
                      This panel had an "Deploy here: [app] [Set as target]" form, and an
                      operator asked the obvious question: this section registers HOSTS, why is
                      it binding apps? They were right, and it was here for a bad reason — the
                      release gate's host picker did not actually bind (it created the target
                      and left host_id NULL), so this was the only control that worked. With
                      the gate fixed, binding belongs where the operator is deciding to ship:
                      on the deploy step, and on the app's own card. Two controls for one
                      relationship, in two places, is how a host ends up disagreeing with the
                      app about where it lives. */}
                  {h.app_count ? null : (
                    <span className="detail">
                      Apps are pointed at a host when you deploy them — choose this host on the
                      deploy step, or on the app under “Apps and where they deploy”.
                    </span>
                  )}

                  <div>
                    <ActionButton busy={busy} label="Re-verify" busyLabel="Connecting…"
                      onClick={() => run(() => callAction(verifyDeploymentHostAction)(null, formOf({ host_id: h.id })))} />
                  </div>
                </div>
              ) : null}

              {/* Where direct host changes are enabled, retirement is available at every stage. */}
              {hostWritesAllowed ? <details className="fleet-retire">
                <summary className="detail">Retire this host…</summary>
                <form style={{ display: "grid", gap: 6, marginTop: 6 }}
                  onSubmit={(e) => {
                    e.preventDefault();
                    const fd = new FormData(e.currentTarget as HTMLFormElement);
                    fd.set("host_id", h.id);
                    for (const k of ["revoke_key", "remove_docker", "purge", "force"]) {
                      fd.set(k, fd.get(k) === "on" ? "true" : "false");
                    }
                    void run(() => callAction(retireHostAction)(null, fd));
                  }}>
                  <span className="detail">
                    Removes only the keys BOSS put on {h.address} (a backup is left beside the
                    file). A key you lent BOSS is never removed.
                  </span>
                  <label className="detail">
                    <input type="checkbox" name="revoke_key" defaultChecked /> revoke BOSS’s key
                    on the host
                  </label>
                  <label className="detail">
                    <input type="checkbox" name="remove_docker" /> also uninstall docker
                    <em> — only if nothing else on this box uses it</em>
                  </label>
                  <label className="detail">
                    <input type="checkbox" name="purge" /> delete the record entirely
                    <em> — otherwise it is kept, marked retired, for the audit trail</em>
                  </label>
                  {h.app_count ? (
                    <label className="detail">
                      <input type="checkbox" name="force" /> detach the {h.app_count} app(s)
                      still pointed here
                    </label>
                  ) : null}
                  <div>
                    <button className="cap-btn tone-red" type="submit" disabled={busy}>
                      Retire host
                    </button>
                  </div>
                </form>
              </details> : null}

              {/* Findings stay visible at every stage: they are the reason for the stage.
                  Render the REMEDY as the sentence and keep the raw detail secondary — the
                  bare detail for a lost credential was an internal store key, which read as a
                  live fault rather than as the explanation for being back at step 1. */}
              {(pf.findings ?? []).length ? (
                <ul className="detail" data-testid="fleet-findings" style={{ margin: 0 }}>
                  {(pf.findings ?? []).map((f, i) => (
                    <li key={i}>
                      <strong>{findingLabel(f.kind)}</strong>: {f.remedy || f.detail}
                      {f.remedy && f.detail ? (
                        <div className="detail" style={{ opacity: 0.6, marginTop: 2 }}>{f.detail}</div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 12 }}>
        <ActionButton busy={busy} label={openReg ? "Cancel" : "Register a host"}
          busyLabel="…" onClick={() => setOpenReg((v) => !v)} />
      </div>

      {openReg ? (
        <form style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}
          onSubmit={(e) => {
            e.preventDefault();
            const fd = new FormData(e.currentTarget as HTMLFormElement);
            fd.set("project_id", projectId);
            void run(() => callAction(registerDeploymentHostAction)(null, fd), () => setOpenReg(false));
          }}>
          <Field label="Name" name="name" placeholder="hetzner-ci-01" required />
          <Field label="Address" name="address" placeholder="192.0.2.202" required />
          <Field label="SSH user" name="ssh_user" defaultValue="root" />
          {/* Left blank on purpose: verify reads `uname -m` from the box and corrects it. */}
          <Field label="Arch (optional)" name="arch" placeholder="detected on verify" />
          {/* BOSS cannot discover this, and getting it wrong fails the push with an error that
              points at the host instead of at the laptop. Blank = "my default ssh works". */}
          <Field label="Your SSH key (optional)" name="admin_identity_file"
                 placeholder="~/.ssh/id_ed25519 — blank if plain ssh works" />
          <button className="cap-btn" type="submit" disabled={busy}>Register</button>
        </form>
      ) : null}
    </section>
  );
}

function formOf(values: Record<string, string>): FormData {
  const fd = new FormData();
  for (const [k, v] of Object.entries(values)) fd.set(k, v);
  return fd;
}
