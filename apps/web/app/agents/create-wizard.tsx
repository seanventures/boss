"use client";

import React, { useEffect, useState } from "react";

import { useGate } from "../../components/gated";
import { apiWrite } from "../../lib/api-write";
import {
  createAgent, errorSentence, loadGrantable, parseToolList,
  type Grantable, type McpGrant,
} from "../../lib/agents";
import { HelpTip } from "../help-tip";
import "./create-wizard.css";

type Step = "kind" | "name" | "grants" | "wake" | "review";
type Kind = "agent" | "bot";
type Wake = "cron" | "event" | "webhook" | "monitor" | "intake";
type Env = "dev" | "staging";

const STEP_LABEL: Record<Step, string> = {
  kind: "Kind", name: "Name", grants: "Grants", wake: "Wake", review: "Review",
};
const STEP_TITLE: Record<Step, string> = {
  kind: "Agent or bot", name: "Name", grants: "Skills and MCP", wake: "Where it wakes", review: "Review",
};
const WAKES: Array<[Wake, string]> = [
  ["cron", "Schedule"], ["event", "Event"], ["webhook", "Webhook"],
  ["monitor", "Watch"], ["intake", "Intake"],
];

function stepsFor(kind: Kind | ""): Step[] {
  return kind === "bot" ? ["kind", "name", "grants", "wake", "review"] : ["kind", "name", "grants"];
}

async function postJson(path: string, body: Record<string, unknown>, press: { scope: string; label: string }) {
  try {
    const response = await apiWrite(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, { press });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return { ok: false as const, error: errorSentence(data, response.status) };
    return { ok: true as const, data };
  } catch (e) {
    return { ok: false as const, error: `The request did not reach the control plane: ${String((e as Error)?.message ?? e)}` };
  }
}

export function AgentBotWizard({ projectId, actor, onClose, onCreated }: {
  projectId: string;
  actor: string;
  onClose: () => void;
  onCreated?: (roleKey: string) => void;
}) {
  const mayCreate = useGate("agent:create", projectId);
  const mayDeploy = useGate("agent:deploy", projectId);
  const [step, setStep] = useState<Step>("kind");
  const [kind, setKind] = useState<Kind | "">("");
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [grantable, setGrantable] = useState<Grantable | null>(null);
  const [loadError, setLoadError] = useState("");
  const [skills, setSkills] = useState<string[]>([]);
  const [servers, setServers] = useState<Record<string, string>>({});
  const [skillFilter, setSkillFilter] = useState("");
  const [wake, setWake] = useState<Wake | "">("");
  const [environment, setEnvironment] = useState<Env>("dev");
  const [cron, setCron] = useState("");
  const [everySeconds, setEverySeconds] = useState("");
  const [cycles, setCycles] = useState("");
  const [eventType, setEventType] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [made, setMade] = useState("");

  useEffect(() => {
    let alive = true;
    void loadGrantable(projectId).then((r) => {
      if (!alive) return;
      if (r.ok) setGrantable(r.data);
      else setLoadError(r.error);
    });
    return () => { alive = false; };
  }, [projectId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const steps = stepsFor(kind);
  const refused = !mayCreate.allowed || (kind === "bot" && !mayDeploy.allowed);
  const gate = kind === "bot" && !mayDeploy.allowed ? mayDeploy : mayCreate;

  const wakeProblem = (): string => {
    if (wake === "webhook") return "";
    if (wake === "event") return eventType.trim() ? "" : "Name the event";
    if (wake === "intake") return connectionId.trim() ? "" : "Name the connection";
    if (wake === "cron" && cron.trim()) return "";
    if (wake === "cron" || wake === "monitor") {
      if (!everySeconds.trim()) return "Say when it wakes";
      const every = Number(everySeconds);
      if (!Number.isInteger(every) || every < 60) return "At least 60 seconds";
      if (wake === "monitor" && cycles.trim()) {
        const n = Number(cycles);
        if (!Number.isInteger(n) || n < 0) return "Cycles is a count";
      }
      return "";
    }
    return "Pick what wakes it";
  };

  const wakePayload = (): { config: Record<string, unknown>; connection_id?: string } | null => {
    if (wakeProblem()) return null;
    if (wake === "cron" && cron.trim()) return { config: { cron: cron.trim(), timezone: "UTC" } };
    if (wake === "cron" || wake === "monitor") {
      const config: Record<string, unknown> = { every_seconds: Number(everySeconds), timezone: "UTC" };
      if (wake === "monitor") config.max_cycles = cycles.trim() ? Number(cycles) : 0;
      return { config };
    }
    if (wake === "event") return { config: { event_type: eventType.trim() } };
    if (wake === "webhook") return { config: {} };
    if (wake === "intake") return { config: { match: {} }, connection_id: connectionId.trim() };
    return null;
  };

  const go = (next: Step) => { setError(""); setStep(next); };

  const forward = () => {
    if (step === "kind") {
      if (!kind) { setError("Pick agent or bot"); return; }
      go("name");
      return;
    }
    if (step === "name") {
      if (!name.trim()) { setError("Name it first"); return; }
      go("grants");
      return;
    }
    if (step === "grants") {
      if (kind === "bot") go("wake");
      else void submit();
      return;
    }
    if (step === "wake") {
      const problem = wakeProblem();
      if (problem) { setError(problem); return; }
      go("review");
    }
  };

  const back = () => {
    const i = steps.indexOf(step);
    if (i > 0) go(steps[i - 1]);
  };

  const submit = async () => {
    if (busy || made || refused) return;
    if (!name.trim()) { setError("Name it first"); setStep("name"); return; }
    const trigger = kind === "bot" ? wakePayload() : null;
    if (kind === "bot" && !trigger) {
      setError(wakeProblem() || "Say when it wakes");
      setStep("wake");
      return;
    }
    setBusy(true);
    setError("");
    const mcpServers: McpGrant[] = Object.entries(servers).map(([server_id, tools]) => ({
      server_id, tools: parseToolList(tools),
    }));
    const created = await createAgent(projectId, actor, {
      name: name.trim(), purpose: purpose.trim(), skills, mcpServers,
    });
    if (!created.ok) { setBusy(false); setError(created.error); return; }
    const key = typeof created.data.role_key === "string" ? created.data.role_key : "";
    if (!key) { setBusy(false); setError("No agent key came back."); return; }
    if (kind !== "bot" || !trigger) { setBusy(false); setMade(key); return; }
    // Spec v1 is what create records. Prod is refused here: it is a promotion.
    // The trigger route 404s until this environment has the deployment.
    const deployed = await postJson(`/v1/agents/${encodeURIComponent(key)}/deployments`, {
      project_id: projectId, actor, environment, version: 1,
    }, { scope: "bot-deploy", label: "Deploy" });
    if (!deployed.ok) { setBusy(false); setError(deployed.error); return; }
    const triggered = await postJson(`/v1/agents/${encodeURIComponent(key)}/triggers`, {
      project_id: projectId,
      actor,
      environment,
      kind: wake,
      config: trigger.config,
      name: name.trim(),
      ...(trigger.connection_id ? { connection_id: trigger.connection_id } : {}),
    }, { scope: "bot-trigger", label: "Add trigger" });
    setBusy(false);
    if (!triggered.ok) { setError(triggered.error); return; }
    setMade(key);
  };

  const last = step === "review" || (step === "grants" && kind !== "bot");
  const q = skillFilter.trim().toLowerCase();
  const shownSkills = (grantable?.skills ?? []).filter((s) => !q
    || `${s.skill_id} ${s.label} ${s.summary}`.toLowerCase().includes(q));

  const wakeLine = () => {
    if (wake === "cron" && cron.trim()) return cron.trim();
    if ((wake === "cron" || wake === "monitor") && everySeconds.trim()) {
      return wake === "monitor" && cycles.trim() ? `Every ${everySeconds}s · ${cycles}` : `Every ${everySeconds}s`;
    }
    if (wake === "event") return eventType.trim();
    if (wake === "webhook") return "Webhook";
    if (wake === "intake") return connectionId.trim();
    return "";
  };

  return (
    <div className="abw-backdrop">
      <section className="abw-dialog" role="dialog" aria-modal="true" aria-labelledby="abw-title"
               data-testid="agent-bot-wizard" data-step={made ? "done" : step}>
        <header className="abw-head">
          <ol className="abw-steps">
            {steps.map((s) => (
              <li key={s} aria-current={s === step && !made ? "step" : undefined}>{STEP_LABEL[s]}</li>
            ))}
          </ol>
          <h2 id="abw-title">{made ? name.trim() : STEP_TITLE[step]}</h2>
          <button type="button" className="abw-close" aria-label="Close" onClick={onClose} disabled={busy}>×</button>
        </header>

        <div className="abw-body">
          {made ? (
            <div data-testid="abw-created" className="abw-done">
              <span className="abw-k">Key</span>
              <code>{made}</code>
            </div>
          ) : step === "kind" ? (
            <div className="abw-kinds" role="group" aria-label="Kind">
              <button type="button" className="abw-kind" data-testid="abw-kind-agent" aria-pressed={kind === "agent"}
                      onClick={() => setKind("agent")}>
                <b>Agent</b>
                <span>Asked for work</span>
              </button>
              <button type="button" className="abw-kind" data-testid="abw-kind-bot" aria-pressed={kind === "bot"}
                      onClick={() => setKind("bot")}>
                <b>Bot</b>
                <span>Wakes itself</span>
              </button>
            </div>
          ) : step === "name" ? (
            <div className="abw-fields">
              <label className="abw-field">
                <span>Name</span>
                <input value={name} autoFocus data-testid="abw-name" aria-invalid={error ? true : undefined}
                       onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="abw-field">
                <span>Purpose <HelpTip label="Purpose" text="BOSS reads this when it picks an agent for a step." /></span>
                <textarea rows={2} value={purpose} data-testid="abw-purpose"
                          onChange={(e) => setPurpose(e.target.value)} />
              </label>
            </div>
          ) : step === "grants" ? (
            <div className="abw-fields">
              {loadError ? <div className="abw-alert" data-testid="abw-load-error">{loadError}</div> : null}
              <fieldset className="abw-group">
                <legend>Skills · {skills.length} <HelpTip label="Skills" text="Granted now. A step can then use them." /></legend>
                {(grantable?.skills.length ?? 0) > 8 ? (
                  <input type="search" className="abw-filter" placeholder="Find a skill" value={skillFilter}
                         onChange={(e) => setSkillFilter(e.target.value)} />
                ) : null}
                <div className="abw-list" data-testid="abw-skills">
                  {shownSkills.map((s) => {
                    const on = skills.includes(s.skill_id);
                    return (
                      <label key={s.skill_id} className={on ? "on" : ""}>
                        <input type="checkbox" checked={on} data-testid={`abw-skill-${s.skill_id}`}
                               onChange={() => setSkills(on ? skills.filter((id) => id !== s.skill_id) : [...skills, s.skill_id])} />
                        <b>{s.skill_id}</b>
                      </label>
                    );
                  })}
                  {grantable && !grantable.skills.length ? <div className="abw-empty">No skills yet</div> : null}
                </div>
              </fieldset>
              <fieldset className="abw-group">
                <legend>MCP · {Object.keys(servers).length} <HelpTip label="MCP" text="Empty means every tool on that server." /></legend>
                <div className="abw-list" data-testid="abw-mcp">
                  {(grantable?.mcp_servers ?? []).map((m) => {
                    const on = m.mcp_server_id in servers;
                    return (
                      <label key={m.mcp_server_id} className={on ? "on" : ""}>
                        <input type="checkbox" checked={on} data-testid={`abw-mcp-${m.mcp_server_id}`}
                               onChange={() => {
                                 const next = { ...servers };
                                 if (on) delete next[m.mcp_server_id]; else next[m.mcp_server_id] = "";
                                 setServers(next);
                               }} />
                        <span>
                          <b>{m.server}</b>
                          {on ? (
                            <input className="abw-tools" placeholder="Only these tools" value={servers[m.mcp_server_id]}
                                   data-testid={`abw-mcp-tools-${m.mcp_server_id}`}
                                   onClick={(e) => e.preventDefault()}
                                   onChange={(e) => setServers({ ...servers, [m.mcp_server_id]: e.target.value })} />
                          ) : null}
                        </span>
                      </label>
                    );
                  })}
                  {grantable && !grantable.mcp_servers.length ? <div className="abw-empty">No MCP servers yet</div> : null}
                </div>
              </fieldset>
            </div>
          ) : step === "wake" ? (
            <div className="abw-fields">
              <div className="abw-wakes" role="group" aria-label="Where it wakes">
                {WAKES.map(([id, label]) => (
                  <button key={id} type="button" className="abw-wake" data-testid={`abw-wake-${id}`}
                          aria-pressed={wake === id} onClick={() => { setWake(id); setError(""); }}>{label}</button>
                ))}
              </div>
              <div className="abw-envs" role="group" aria-label="Environment">
                <span>Where <HelpTip label="Where" text="Dev or staging. Production is a promotion." /></span>
                {(["dev", "staging"] as const).map((env) => (
                  <button key={env} type="button" className="abw-wake" data-testid={`abw-env-${env}`}
                          aria-pressed={environment === env} onClick={() => setEnvironment(env)}>
                    {env === "dev" ? "Dev" : "Staging"}
                  </button>
                ))}
              </div>
              {wake === "cron" || wake === "monitor" ? (
                <div className="abw-fields">
                  {wake === "cron" ? (
                    <label className="abw-field">
                      <span>Cron <HelpTip label="Cron" text="A cron expression, or seconds between wakes. At least 60." /></span>
                      <input value={cron} data-testid="abw-cron" autoFocus placeholder="15 * * * *"
                             onChange={(e) => setCron(e.target.value)} />
                    </label>
                  ) : null}
                  <label className="abw-field">
                    <span>{wake === "monitor" ? "Every seconds" : "Or every seconds"}</span>
                    <input value={everySeconds} data-testid="abw-every" inputMode="numeric"
                           onChange={(e) => setEverySeconds(e.target.value)} />
                  </label>
                  {wake === "monitor" ? (
                    <label className="abw-field">
                      <span>Cycles <HelpTip label="Cycles" text="How many checks. 0 keeps going." /></span>
                      <input value={cycles} data-testid="abw-cycles" inputMode="numeric"
                             onChange={(e) => setCycles(e.target.value)} />
                    </label>
                  ) : null}
                </div>
              ) : null}
              {wake === "event" ? (
                <label className="abw-field">
                  <span>Event <HelpTip label="Event" text="A BOSS event, such as github.issue_opened." /></span>
                  <input value={eventType} data-testid="abw-event" autoFocus
                         onChange={(e) => setEventType(e.target.value)} />
                </label>
              ) : null}
              {wake === "webhook" ? (
                <div className="abw-note">Signed URL <HelpTip label="Webhook" text="The secret is shown once, when the trigger is saved." /></div>
              ) : null}
              {wake === "intake" ? (
                <label className="abw-field">
                  <span>Connection <HelpTip label="Connection" text="The intake connection whose events wake it." /></span>
                  <input value={connectionId} data-testid="abw-connection" autoFocus
                         onChange={(e) => setConnectionId(e.target.value)} />
                </label>
              ) : null}
            </div>
          ) : (
            <dl className="abw-review">
              <div><dt>Kind</dt><dd>{kind === "bot" ? "Bot" : "Agent"}</dd></div>
              <div><dt>Name</dt><dd data-user-text>{name.trim()}</dd></div>
              {purpose.trim() ? <div><dt>Purpose</dt><dd data-user-text>{purpose.trim()}</dd></div> : null}
              <div><dt>Skills</dt><dd>{skills.length}</dd></div>
              <div><dt>MCP</dt><dd>{Object.keys(servers).length}</dd></div>
              <div><dt>Wakes</dt><dd data-user-text>{wakeLine()}</dd></div>
              <div><dt>Where</dt><dd>{environment}</dd></div>
            </dl>
          )}
          {error ? <div role="alert" className="abw-alert" data-testid="abw-error">{error}</div> : null}
        </div>

        <footer className="abw-foot">
          {made ? (
            <>
              <button type="button" className="abw-btn" onClick={onClose}>Close</button>
              {onCreated ? (
                <button type="button" className="abw-btn abw-btn-primary" onClick={() => onCreated(made)}>Open</button>
              ) : null}
            </>
          ) : (
            <>
              {step === "kind" ? <span /> : (
                <button type="button" className="abw-btn" data-testid="abw-back" onClick={back} disabled={busy}>Back</button>
              )}
              <button type="button" className="abw-btn abw-btn-primary" disabled={busy}
                      data-testid={last ? "abw-create" : "abw-next"}
                      {...(refused ? gate.props : { "data-permission": gate.props["data-permission"], "data-can": "yes" as const })}
                      onClick={() => { if (refused) return; if (last) void submit(); else forward(); }}>
                {busy ? "Working" : last ? "Create" : "Next"}
                {refused ? gate.sr : null}
              </button>
            </>
          )}
        </footer>
      </section>
    </div>
  );
}
