"use client";

/**
 * Managing delivery: onboard an app, hold its credentials, decide when it ships.
 *
 * Everything here replaces a step someone previously ran by hand — hand-written SQL to register
 * a target, `ssh-keygen` in a terminal, `mkdir`/`chown` over SSH, hunting for a free port, and
 * a curl to start the deployment. The wizard is ordered the way onboarding actually fails:
 * credentials before hosts, hosts before ports, and a live preflight before anyone is told it
 * worked.
 *
 * Two rules the UI enforces rather than merely documents:
 *   - a secret VALUE is never displayed, and the only key material ever shown is a PUBLIC half;
 *   - the wizard will not call itself finished while preflight still reports problems, because
 *     discovering a missing credential by watching a deployment fail is the exact experience
 *     this is meant to remove.
 */

import { useEffect, useState, useTransition } from "react";

import {
  bindSecretAction, bootstrapTargetAction, createTargetAction, deployObservabilityAction,
  getDeliveryDefaultsAction, listRefsAction, listRepoAppsAction, listReposAction,
  purgeTargetAction,
  deployTargetAction,
  generateKeypairAction, preflightTargetAction, putSecretAction, reactivateTargetAction,
  setAutoDeployAction,
  teardownTargetAction, verifySecretAction,
} from "./actions";
import { LiveDeployment } from "./deploy-ui";

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
const input: React.CSSProperties = {
  fontSize: 12, padding: "5px 8px", borderRadius: 6, background: "rgba(255,255,255,.04)",
  border: "1px solid rgba(255,255,255,.14)", color: "inherit", minWidth: 150,
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
function ActionButton({ busy, label, busyLabel, danger, onClick, title, style }: {
  busy?: boolean; label: string; busyLabel?: string; danger?: boolean;
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
      disabled={!!busy}
      onClick={onClick}
      style={{
        ...btn,
        ...(danger ? { borderColor: "rgba(248,81,73,.5)", color: "#f85149" } : {}),
        ...(busy ? { opacity: 0.75, cursor: "progress" } : {}),
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
      <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>{label}</span>
      <input style={input} {...rest} />
    </label>
  );
}

// --------------------------------------------------------------------------- //
// Secrets
// --------------------------------------------------------------------------- //
export function SecretsPanel({ initial }: { initial?: SecretsView | null }) {
  const [pending, start] = useTransition();
  const [msg, setMsg] = useState<string | null>(null);
  const [publicKey, setPublicKey] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [kind, setKind] = useState("api_token");
  const [backend, setBackend] = useState("platform_secret_store");

  const secrets = initial?.secrets ?? [];

  const upload = () => start(async () => {
    const gaps = missingFields({ name, value });
    if (gaps.length) { setMsg(`fill in: ${gaps.join(", ")}`); return; }
    setMsg("uploading…"); setPublicKey(null);
    const out = await putSecretAction({ name, value, secret_kind: kind, backend });
    // The value is cleared immediately: it has been written through, and keeping it in a form
    // field is keeping a secret in the browser for no reason.
    setValue("");
    setMsg(out?.error ? `failed: ${out.error}` : `stored ${out.name} (${out.fingerprint || out.last4 || "recorded"})`);
  });

  const keygen = () => start(async () => {
    const gaps = missingFields({ name });
    if (gaps.length) { setMsg(`fill in: ${gaps.join(", ")}`); return; }
    setMsg("generating…"); setPublicKey(null);
    const out = await generateKeypairAction({ name, backend });
    if (out?.error) { setMsg(`failed: ${out.error}`); return; }
    setPublicKey(out.public_key);
    setMsg(`generated ${out.fingerprint} — install the PUBLIC key below on the host`);
  });

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
          <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>kind</span>
          <select style={input} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="api_token">api_token</option>
            <option value="ssh_private_key">ssh_private_key</option>
            <option value="kubeconfig">kubeconfig</option>
            <option value="access_key">access_key</option>
          </select>
        </label>
        <label style={{ display: "grid", gap: 3 }}>
          <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>backend</span>
          <select style={input} value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="platform_secret_store">platform_secret_store</option>
            <option value="file">file</option>
            <option value="env">env (read-only)</option>
          </select>
        </label>
        <Field label="value (write-only)" type="password" value={value}
               placeholder="paste — never shown again"
               onChange={(e) => setValue(e.target.value)} />
        <button style={btn} disabled={pending} onClick={upload}>Upload</button>
        <button style={btn} disabled={pending} onClick={keygen}
                title="BOSS generates the pair, keeps the private half, and shows you the public one">
          Generate keypair
        </button>
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
                        onClick={() => start(async () => {
                          const out = await verifySecretAction(s.secret_ref_id);
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

export function MonitoringPanel({ initial, hostTargetId }: {
  initial?: Observability | null; hostTargetId?: string;
}) {
  const [pending, start] = useTransition();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const data = initial;

  if (!data || data.error) {
    return (
      <section className="ct-block">
        <div className="ct-block-title">Monitoring</div>
        <div className="detail" style={{ color: data?.error ? "#f85149" : undefined }}>
          {data?.error ? `Monitoring: ${data.error}` : "Loading…"}
        </div>
      </section>
    );
  }

  const running = data.status?.running;
  return (
    <section className="ct-block">
      <div className="ct-block-title">
        Monitoring{" "}
        <span style={{ fontSize: 12,
                       color: running ? "#3fb950" : running === false ? "#d29922" : "#8b949e" }}>
          ● {running ? "stack running" : running === false ? "stack not running" : "unknown"}
        </span>
      </div>

      <div className="detail" style={{ marginBottom: 10 }}>
        One OpenTelemetry collector, one Prometheus and one Grafana for every app.
        {" "}The scrape config is generated from the deployment targets below — onboarding an app
        is what puts it on a dashboard, so the two cannot drift.
      </div>

      <div style={{ ...box, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <ActionButton busy={!!busy}
                label={data.deployed ? "Redeploy stack" : "Deploy monitoring stack"}
                busyLabel="Deploying stack"
                title="Regenerate the config from the registry and deploy"
                onClick={() => start(async () => {
                  if (!hostTargetId) {
                    setMsg("no active deployment target — the stack borrows a host and "
                           + "credential from one, so onboard an app first");
                    return;
                  }
                  setMsg(null); setBusy("Deploying stack");
                  let out: any;
                  try { out = await deployObservabilityAction(hostTargetId!); }
                  finally { setBusy(null); }
                  setMsg(out?.error
                    ? `failed: ${typeof out.error === "string" ? out.error : JSON.stringify(out.error).slice(0, 200)}`
                    : `stack deployed — ${out.apps?.length ?? 0} app(s) on ${out.grafana_url}`);
                })} />
        {data.grafana_url && (
          <a href={data.grafana_url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            Open Grafana ↗
          </a>
        )}
        {busy && <Working label={busy} />}
        {msg && <span className="detail" style={{ fontSize: 12 }}>{msg}</span>}
      </div>

      <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 12 }}>
        <thead><tr style={{ textAlign: "left", opacity: .7 }}>
          <th>app</th><th>environment</th><th>scraped at</th><th>dashboard</th>
        </tr></thead>
        <tbody>
          {data.apps.length === 0 && (
            <tr><td colSpan={4} className="detail">
              No active deployment targets, so there is nothing to monitor yet.
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

      {data.status?.containers && data.status.containers.length > 0 && (
        <div className="detail" style={{ fontSize: 11, marginTop: 8 }}>
          {data.status.containers.map((c) => `${c.name}: ${c.status}`).join(" · ")}
        </div>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Onboarding wizard
// --------------------------------------------------------------------------- //
export function OnboardWizard({ secrets }: { secrets: Secret[] }) {
  const [open, setOpen] = useState(false);
  const [pending, start] = useTransition();
  const [step, setStep] = useState(1);
  const [log, setLog] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
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
    getDeliveryDefaultsAction().then((d) => {
      if (!live || !d || d.error) return;
      setDefaults(d);
      // Set the VALUE, not a placeholder. Showing the default repo as placeholder text made an
      // empty field look filled — an operator typed nothing because it already read
      // "boss-sandbox", then got "fill in: repo" for a field that appeared complete.
      setForm((f) => (f.repo ? f : { ...f, repo: d.repo || "" }));
    });
    // BOSS already knows every repo it has worked in — asking an operator to type one from
    // memory is both unnecessary and a chance to typo a name that fails later at clone time.
    listReposAction().then((d) => { if (live && d?.repos) setRepos(d.repos); });
    return () => { live = false; };
  }, []);
  const [targetId, setTargetId] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<{ ready: boolean; problems: string[] } | null>(null);

  const [form, setForm] = useState({
    app_name: "", repo: "", environment: "staging", target_kind: "ssh",
    target_ref: "", domain: "", scheme: "http", app_port: "", app_entry: "",
    publishable_ports: "",
    secret_ref_id: "", auto_deploy: false,
  });
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }));
  const say = (line: string) => setLog((l) => [...l, line]);

  const create = () => start(async () => {
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
      say("host must include the ssh user, e.g. ubuntu@3.94.66.139");
      return;
    }
    if (appsAmbiguous && !form.app_entry) {
      say(`this repo contains ${apps.length} runnable apps — choose which one this target is, `
          + "or the deploy cannot tell them apart");
      return;
    }
    setBusy("Creating target");
    try {
    say("registering target…");
    const out = await createTargetAction({
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
      const bound = await bindSecretAction(out.id, "ssh_key", form.secret_ref_id);
      say(bound?.error ? `binding failed: ${bound.error}` : "credential bound");
    }
    setStep(2);
    } finally { setBusy(null); }
  });

  const bootstrap = () => start(async () => {
    setBusy("Preparing host");
    try {
    if (!targetId) return;
    say("preparing the host…");
    const out = await bootstrapTargetAction(targetId, true);
    if (out?.error) { say(`failed: ${out.error}`); return; }
    (out.changed || []).forEach((c: string) => say(`✓ ${c}`));
    (out.problems || []).forEach((p: string) => say(`✗ ${p}`));
    if (out.applied_port) say(`port ${out.applied_port} written to the target`);
    setStep(3);
    } finally { setBusy(null); }
  });

  const check = () => start(async () => {
    setBusy("Running preflight");
    try {
    if (!targetId) return;
    say("preflight…");
    const out = await preflightTargetAction(targetId);
    setPreflight(out);
    (out.problems || []).forEach((p: string) => say(`✗ ${p}`));
    if (out.ready) say("✓ ready to deploy");
    } finally { setBusy(null); }
  });

  const finish = () => start(async () => {
    if (!targetId) return;
    if (form.auto_deploy) {
      await setAutoDeployAction(targetId, { auto_deploy: true });
      say("auto-deploy on: merges to this repo will ship automatically");
    }
    say("onboarded — use Deploy to ship it now");
  });

  if (!open) {
    return (
      <button style={{ ...btn, padding: "6px 14px" }} onClick={() => setOpen(true)}>
        + Onboard an app
      </button>
    );
  }

  return (
    <section className="ct-block">
      <div className="ct-block-title">
        Onboard an app{" "}
        <span className="detail" style={{ fontSize: 12 }}>step {step} of 4</span>
      </div>

      {step === 1 && (
        <div style={{ display: "grid", gap: 10 }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <Field label="app name" value={form.app_name}
                   onChange={(e) => set("app_name", e.target.value)} placeholder="stock-app" />
            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>repo</span>
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
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>environment</span>
              <select style={input} value={form.environment}
                      onChange={(e) => set("environment", e.target.value)}>
                <option value="staging">staging (deploys automatically)</option>
                <option value="dev">dev</option>
                <option value="preview">preview</option>
                <option value="production">production (human-gated)</option>
              </select>
            </label>
          </div>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>destination</span>
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
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>health scheme</span>
              <select style={input} value={form.scheme}
                      onChange={(e) => set("scheme", e.target.value)}>
                <option value="http">http</option>
                <option value="https">https</option>
              </select>
            </label>
            <Field label="port (blank = BOSS picks)" value={form.app_port}
                   onChange={(e) => set("app_port", e.target.value)} placeholder="auto" />
            {/* Free on the host is not the same as reachable from the internet. Without this,
                BOSS picks a port the cloud firewall drops: the app runs, the health check times
                out, and nothing in the failure names the security group. */}
            <Field label="ports this host publishes (optional)"
                   title="Comma-separated, ranges allowed — e.g. 3003,4000,10000-10001. BOSS will only pick from these."
                   value={form.publishable_ports}
                   onChange={(e) => set("publishable_ports", e.target.value)}
                   placeholder="3003,4000,10000-10001" />
            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>
                app entry {appsAmbiguous ? "(required — this repo has several)" : "(optional)"}
              </span>
              <select style={input} value={form.app_entry}
                      onFocus={() => {
                        if (apps.length || !form.repo) return;
                        const [o, n] = form.repo.includes("/")
                          ? form.repo.split("/") : [defaults.owner || "", form.repo];
                        listRepoAppsAction(o, n).then((d) => {
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
            <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>
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
            BOSS will create the deploy directory, confirm docker, and choose a port nothing
            else on that host is using.
          </div>
          <div>
            <ActionButton label="Prepare host →" busyLabel="Preparing host"
                          busy={busy === "Preparing host"} onClick={bootstrap} />
          </div>
        </div>
      )}

      {step === 3 && (
        <div style={{ display: "grid", gap: 10 }}>
          <div className="detail">
            Preflight resolves the credential, reaches the host, and checks the deploy
            directory — everything that would otherwise fail mid-deployment.
          </div>
          <div>
            <ActionButton label="Run preflight" busyLabel="Running preflight"
                          busy={busy === "Running preflight"} onClick={check} />
            <button style={{ ...btn, marginLeft: 8, opacity: preflight?.ready ? 1 : 0.65 }}
                    disabled={pending}
                    onClick={() => {
                      // Same rule as Destroy: the click explains itself. A disabled button
                      // that only speaks through a tooltip reads as a broken one.
                      if (!preflight?.ready) {
                        say(preflight
                          ? "preflight still reports problems — fix those first"
                          : "run preflight first");
                        return;
                      }
                      setStep(4);
                    }}>
              Continue →
            </button>
          </div>
        </div>
      )}

      {step === 4 && (
        <div style={{ display: "grid", gap: 10 }}>
          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
            <input type="checkbox" checked={form.auto_deploy}
                   onChange={(e) => set("auto_deploy", e.target.checked)} />
            Deploy automatically when a PR merges to <code>{form.repo}</code>
          </label>
          <div className="detail" style={{ fontSize: 12 }}>
            Off by default. You can turn it on later, and pause it during an incident without
            losing the setting.
          </div>
          <div>
            <button style={btn} disabled={pending} onClick={finish}>Finish</button>
            <button style={{ ...btn, marginLeft: 8 }} onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>
      )}

      {busy && <div style={{ marginTop: 10 }}><Working label={busy} /></div>}

      {log.length > 0 && (
        <pre style={{ marginTop: 12, fontSize: 11, background: "rgba(255,255,255,.04)",
                      padding: 10, borderRadius: 6, maxHeight: 200, overflow: "auto" }}>
          {log.join("\n")}
        </pre>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Per-target trigger controls
// --------------------------------------------------------------------------- //
/** Removing an app. Kept behind a disclosure and a typed name, because a destructive action
 *  reachable by one click is a destructive action taken by accident. */
function TeardownControls({ target }: { target: { id: string; app_name: string } }) {
  const [open, setOpen] = useState(false);
  const [pending, start] = useTransition();
  const [confirm, setConfirm] = useState("");
  const [withData, setWithData] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Which action is in flight, so the indicator says "Destroying" rather than a generic
  // "working" that could mean either verb.
  const [busy, setBusy] = useState<string | null>(null);

  const run = (mode: "stop" | "destroy") => start(async () => {
    // Validate on CLICK rather than by disabling the button. A disabled control that explains
    // itself only in a tooltip is indistinguishable from a broken one: the click does nothing
    // and says nothing, which is exactly how this read the first time someone used it.
    if (mode === "destroy" && confirm !== target.app_name) {
      setMsg(`type “${target.app_name}” in the box to confirm — this is deliberate friction, `
             + `because removing the wrong app is not undoable`);
      return;
    }
    setMsg(null);
    setBusy(mode === "stop" ? "Stopping" : (withData ? "Destroying + data" : "Destroying"));
    // finally, not a trailing call: an indicator that never stops asserts work is happening
    // when it is not, which is a worse lie than showing nothing.
    let out: any;
    try {
      out = await teardownTargetAction(target.id, {
        stop_only: mode === "stop",
        include_data: mode === "destroy" && withData,
        confirm: mode === "stop" ? undefined : confirm,
      });
    } finally {
      setBusy(null);
    }
    if (out?.error) { setMsg(`failed: ${typeof out.error === "string" ? out.error : JSON.stringify(out.error).slice(0, 160)}`); return; }
    const freed = out.reclaimed_kb ? `${(out.reclaimed_kb / 1024).toFixed(1)} MB reclaimed` : "";
    setMsg(`${(out.removed || []).join("; ")}${freed ? ` — ${freed}` : ""}`
      + (out.data_kept_kb ? ` (data kept: ${(out.data_kept_kb / 1024).toFixed(1)} MB)` : ""));
    setConfirm("");
  });

  if (!open) {
    return <button style={{ ...btn, borderColor: "rgba(248,81,73,.4)" }}
                   onClick={() => setOpen(true)}>Remove…</button>;
  }
  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <ActionButton label="Stop" busyLabel="Stopping" busy={busy === "Stopping"}
                    title="Stop the container; files and data stay, so a redeploy brings it back"
                    onClick={() => run("stop")} />
      <input style={{ ...input, minWidth: 130 }} value={confirm}
             placeholder={`type ${target.app_name}`}
             onChange={(e) => setConfirm(e.target.value)} />
      <label style={{ display: "flex", gap: 5, alignItems: "center", fontSize: 12 }}>
        <input type="checkbox" checked={withData}
               onChange={(e) => setWithData(e.target.checked)} />
        also delete data
      </label>
      {/* Not disabled for a missing confirmation: the click is what surfaces the reason. It IS
          disabled while running, so a second click cannot fire a second teardown. */}
      <ActionButton danger busy={!!busy && busy !== "Stopping"}
                    label={withData ? "Destroy + data" : "Destroy"}
                    busyLabel={withData ? "Destroying + data" : "Destroying"}
                    style={{ opacity: confirm === target.app_name ? 1 : 0.65 }}
                    onClick={() => run("destroy")} />
      <button style={btn} disabled={pending} onClick={() => setOpen(false)}>Cancel</button>
      {msg && <span className="detail" style={{ fontSize: 12 }}>{msg}</span>}
    </span>
  );
}

export function TargetControls({ target, shipped }: {
  shipped?: boolean;
  target: { id: string; app_name: string; status?: string; repo?: string;
            repo_owner?: string | null; auto_deploy?: boolean; paused?: boolean };
}) {
  const [pending, start] = useTransition();
  const [state, setState] = useState({ auto: !!target.auto_deploy, paused: !!target.paused });
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");
  const [ref, setRef] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
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
    if (!owner) owner = (await getDeliveryDefaultsAction())?.owner || "";
    if (!owner) return;
    const d = await listRefsAction(owner, name);
    if (d?.branches) setRefs(d.branches);
    if (!ref && d?.default_branch) setRef(d.default_branch);
  };

  // A retired target cannot be deployed — the API refuses with 409 — so deploy controls would
  // only fail. But it is NOT gone: destroy retires rather than deletes, so the credential
  // binding, port, domain and health check all survive. Bringing it back is one click, not a
  // second trip through onboarding, and telling someone to re-enter what BOSS already knows is
  // how a "safe" delete becomes one nobody dares use.
  if (target.status && target.status !== "active") {
    return (
      <span style={{ display: "inline-flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span className="detail" style={{ fontSize: 12 }}>
          {target.status} — its configuration is kept
        </span>
        <ActionButton label="Restore" busyLabel="Restoring" busy={busy === "Restoring"}
                title="Reactivate, re-prepare the host, and deploy again"
                onClick={() => start(async () => {
                  setMsg(null); setBusy("Restoring");
                  let back: any, boot: any;
                  try {
                  back = await reactivateTargetAction(target.id);
                  if (back?.error) { setMsg(`failed: ${back.error}`); return; }
                  // The host directories went with the destroy, so they have to be recreated
                  // before anything can be transferred into them.
                  boot = await bootstrapTargetAction(target.id, false);
                  if (boot?.error || boot?.problems?.length) {
                    setMsg(`restored, but the host needs attention: `
                           + `${boot?.error || boot.problems.join("; ")}`);
                    return;
                  }
                  setMsg("restored and host prepared — press Deploy to ship it again");
                  } finally { setBusy(null); }
                })} />
        {/* Restore reuses the configuration and history; delete throws both away so the name
            is free again. Both are offered because "start from scratch" is a legitimate intent
            that restore cannot express. */}
        <input style={{ ...input, minWidth: 130 }} value={confirm}
               placeholder={`type ${target.app_name}`}
               onChange={(e) => setConfirm(e.target.value)} />
        <ActionButton danger label="Delete permanently" busyLabel="Deleting"
                busy={busy === "Deleting"}
                style={{ opacity: confirm === target.app_name ? 1 : 0.65 }}
                title="Delete this target and its deployment history so the name can be reused"
                onClick={() => start(async () => {
                  if (confirm !== target.app_name) {
                    setMsg(`type “${target.app_name}” to delete it permanently — its deployment `
                           + `history goes with it`);
                    return;
                  }
                  setMsg(null); setBusy("Deleting");
                  let out: any;
                  try { out = await purgeTargetAction(target.id, confirm); }
                  finally { setBusy(null); }
                  setMsg(out?.error
                    ? `failed: ${typeof out.error === "string" ? out.error : JSON.stringify(out.error).slice(0, 180)}`
                    : `deleted — ${out.deployment_runs_deleted} run(s) and `
                      + `${out.health_samples_deleted} health sample(s) removed. The name is free.`);
                })} />
        {msg && <span className="detail" style={{ fontSize: 12 }}>{msg}</span>}
      </span>
    );
  }

  const toggle = (patch: { auto_deploy?: boolean; paused?: boolean }) => start(async () => {
    const out = await setAutoDeployAction(target.id, patch);
    if (out?.error) { setMsg(out.error); return; }
    setState({ auto: !!out.auto_deploy, paused: !!out.paused });
    setMsg(null);
  });

  return (
   <div style={{ display: "grid", gap: 8 }}>
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12 }}>
      <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={state.auto} disabled={pending}
               onChange={(e) => toggle({ auto_deploy: e.target.checked })} />
        deploy on merge
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
      {/* Deploy no longer BLOCKS until the deployment ends, so this button no longer reports
          the outcome — it hands over to the live stepper below, which does. Reporting
          `out.status` here now would print "started" and stop, which is how a working deploy
          came to look like a button that does nothing. */}
      {/* One action, two honest names. Deploying a target that is already running REPLACES
          what is there — a new run, so the previous one keeps its history — and calling that
          "Deploy" invites the question "is there a redeploy somewhere else?". There is not. */}
      <ActionButton label={shipped ? "Redeploy" : "Deploy"} busyLabel="Starting"
              title={shipped
                ? "Ship this target again — a new deployment run at the branch or sha above"
                : "Ship this target for the first time"}
              busy={busy === "Deploying"}
              onClick={() => start(async () => {
                setMsg(null); setBusy("Deploying"); setRunId(null);
                let out: any;
                try {
                  out = await deployTargetAction(target.id, ref);
                } finally { setBusy(null); }
                if (out?.error || !out?.deployment_run_id) {
                  setMsg(`failed: ${out?.error || "no deployment was started"}`);
                  return;
                }
                setRunId(out.deployment_run_id);
              })} />
      <TeardownControls target={target} />
      {msg && <span className="detail">{msg}</span>}
    </div>
    {runId && (
      <div style={{ maxWidth: 760 }}>
        <LiveDeployment runId={runId} />
      </div>
    )}
   </div>
  );
}
