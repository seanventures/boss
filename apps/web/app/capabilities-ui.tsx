import type { NativeTool, McpServerWithTools, SkillSummary } from "../lib/capabilities";
import {
  deleteMcpServerAction,
  registerMcpServerAction,
  setMcpServerEnabledAction,
} from "./actions";

function riskLabel(tier: number): { label: string; tone: string } {
  if (tier <= 0) return { label: "read-only", tone: "muted" };
  if (tier === 1) return { label: "tier 1 · auto", tone: "blue" };
  return { label: `tier ${tier} · approval`, tone: "amber" };
}

export function CapabilitiesView({
  projectId,
  nativeTools,
  skills,
  mcpServers,
  apiReachable,
  engineLabel,
  engineModel,
}: {
  projectId: string;
  nativeTools: NativeTool[];
  skills: SkillSummary[];
  mcpServers: McpServerWithTools[];
  apiReachable: boolean;
  engineLabel?: string;
  engineModel?: string;
}) {
  const reads = nativeTools.filter((t) => t.kind === "read");
  const writes = nativeTools.filter((t) => t.kind === "write");
  const totalMcpTools = mcpServers.reduce((n, s) => n + (s.tools?.length ?? 0), 0);

  // group skills by kind for the registry view
  const skillsByKind = new Map<string, SkillSummary[]>();
  for (const s of skills ?? []) {
    const arr = skillsByKind.get(s.kind) ?? [];
    arr.push(s);
    skillsByKind.set(s.kind, arr);
  }
  const skillKinds = [...skillsByKind.keys()].sort();

  return (
    <div className="cap">
      <p className="cap-intro">
        Every capability an agent can use, in one catalog. <strong>Native tools</strong> are built into
        BOSS&apos;s governed loop; <strong>MCP servers</strong> are pluggable tool backends any agent can
        call. Humans audit and extend them here; agents discover them automatically at run time.
      </p>

      {engineLabel ? (
        <div className="cap-engine">
          <span className="cap-dot live" />
          <span className="cap-engine-label">Reasoning engine</span>
          <strong className="cap-engine-name">{engineLabel}</strong>
          {engineModel ? <code className="cap-engine-model">{engineModel}</code> : null}
          <span className="cap-engine-note">BOSS drives the loop · this backend serves each completion</span>
        </div>
      ) : null}

      <div className="cap-stat-row">
        <div className="cap-stat"><span className="cap-stat-n">{nativeTools.length}</span><span>native tools</span></div>
        <div className="cap-stat"><span className="cap-stat-n">{skills.length}</span><span>internal skills</span></div>
        <div className="cap-stat"><span className="cap-stat-n">{mcpServers.length}</span><span>MCP servers</span></div>
        <div className="cap-stat"><span className="cap-stat-n">{totalMcpTools}</span><span>MCP tools</span></div>
        <div className="cap-stat"><span className="cap-stat-n">{mcpServers.filter((s) => s.enabled && s.reachable).length}</span><span>live &amp; enabled</span></div>
      </div>

      {/* ---- Native tools ---- */}
      <div className="cap-section-head">
        <h3>Native tools</h3>
        <span className="cap-section-sub">Built-in · governed by risk tier</span>
      </div>
      <div className="cap-grid">
        {[...reads, ...writes].map((t) => {
          const r = riskLabel(t.risk_tier);
          return (
            <div key={t.name} className="cap-tool">
              <div className="cap-tool-top">
                <code className="cap-tool-name">{t.name}</code>
                <span className={`cap-badge tone-${t.kind === "read" ? "muted" : "accent"}`}>{t.kind}</span>
                <span className={`cap-badge tone-${r.tone}`}>{r.label}</span>
              </div>
              <p className="cap-tool-desc">{t.description}</p>
              {t.parameters.length > 0 ? (
                <div className="cap-params">
                  {t.parameters.map((p) => (
                    <span key={p.name} className="cap-param">
                      <code>{p.name}</code>
                      <span className="cap-param-type">{p.type}{p.required ? "*" : ""}</span>
                    </span>
                  ))}
                </div>
              ) : (
                <div className="cap-params"><span className="cap-param-none">no parameters</span></div>
              )}
            </div>
          );
        })}
      </div>

      {/* ---- Internal skills registry ---- */}
      <div className="cap-section-head">
        <h3>Internal skills</h3>
        <span className="cap-section-sub">Governed capability units the execution plane runs · shared across agents</span>
      </div>
      {skills.length === 0 ? (
        <p className="detail">No skills registered.</p>
      ) : (
        skillKinds.map((kind) => (
          <div key={kind} className="cap-skill-group">
            <div className="cap-skill-kind">{kind} <span className="wf-count">{skillsByKind.get(kind)!.length}</span></div>
            <div className="cap-grid">
              {skillsByKind.get(kind)!.map((s) => {
                const r = riskLabel(s.risk_tier);
                const callers = (s.permissions?.allowed_callers as string[] | undefined) ?? [];
                const readOnly = (s.policy as { read_only?: boolean } | undefined)?.read_only;
                return (
                  <div key={s.id} className="cap-tool">
                    <div className="cap-tool-top">
                      <code className="cap-tool-name">{s.id}</code>
                      <span className={`cap-badge tone-${r.tone}`}>{r.label}</span>
                      <span className="cap-badge tone-muted">{s.data_boundary}</span>
                      {readOnly ? <span className="cap-badge tone-green">read-only</span> : null}
                      {s.mock_mode ? <span className="cap-badge tone-amber">mock</span> : null}
                    </div>
                    <p className="cap-tool-desc">{s.description}</p>
                    <div className="cap-params">
                      {Object.keys(s.input_schema ?? {}).map((k) => (
                        <span key={k} className="cap-param"><code>{k}</code></span>
                      ))}
                      {Object.keys(s.input_schema ?? {}).length === 0 ? (
                        <span className="cap-param-none">no inputs</span>
                      ) : null}
                    </div>
                    {callers.length > 0 ? (
                      <span className="cap-skill-callers">callable by: {callers.join(", ")}</span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        ))
      )}

      {/* ---- MCP servers ---- */}
      <div className="cap-section-head">
        <h3>MCP servers</h3>
        <span className="cap-section-sub">Pluggable tool backends · JSON-RPC over HTTP · plug in external &amp; org-specific servers</span>
      </div>
      {!apiReachable ? (
        <p className="detail">Capabilities API unreachable.</p>
      ) : mcpServers.length === 0 ? (
        <p className="detail">No MCP servers registered yet. Add one below.</p>
      ) : (
        <div className="cap-servers">
          {mcpServers.map((s) => (
            <div key={s.id} className={`cap-server${s.enabled ? "" : " disabled"}`}>
              <div className="cap-server-head">
                <span className={`cap-dot ${s.reachable ? "live" : "down"}`} />
                <span className="cap-server-name">{s.name}</span>
                <span className={`cap-badge tone-${s.enabled ? "green" : "muted"}`}>{s.enabled ? "enabled" : "disabled"}</span>
                <span className={`cap-badge tone-${s.reachable ? "blue" : "red"}`}>{s.reachable ? "reachable" : "unreachable"}</span>
                <span className="cap-badge tone-muted">risk {s.risk_tier}</span>
                <span className="cap-badge tone-muted">{s.data_boundary}</span>
                <div className="cap-server-actions">
                  <form action={setMcpServerEnabledAction}>
                    <input type="hidden" name="project_id" value={projectId} />
                    <input type="hidden" name="server_id" value={s.id} />
                    <input type="hidden" name="enabled" value={s.enabled ? "false" : "true"} />
                    <button type="submit" className="cap-btn">{s.enabled ? "Disable" : "Enable"}</button>
                  </form>
                  <form action={deleteMcpServerAction}>
                    <input type="hidden" name="project_id" value={projectId} />
                    <input type="hidden" name="server_id" value={s.id} />
                    <button type="submit" className="cap-btn danger">Remove</button>
                  </form>
                </div>
              </div>
              <code className="cap-server-url">{s.url}</code>
              {s.description ? <p className="cap-server-desc">{s.description}</p> : null}
              {s.error ? <p className="cap-server-err">{s.error}</p> : null}
              {s.tools.length > 0 ? (
                <div className="cap-server-tools">
                  {s.tools.map((tool) => (
                    <div key={tool.name} className="cap-mcp-tool">
                      <code className="cap-mcp-tool-name">{tool.name}</code>
                      <span className="cap-mcp-tool-desc">{tool.description}</span>
                    </div>
                  ))}
                </div>
              ) : s.reachable ? (
                <p className="cap-server-desc">No tools advertised.</p>
              ) : null}
            </div>
          ))}
        </div>
      )}

      {/* ---- Add server ---- */}
      <div className="cap-section-head">
        <h3>Add an MCP server</h3>
        <span className="cap-section-sub">Register a new tool backend for agents to use</span>
      </div>
      <form action={registerMcpServerAction} className="cap-add">
        <input type="hidden" name="project_id" value={projectId} />
        <div className="cap-add-row">
          <label>Name<input name="name" placeholder="e.g. web-intel" required /></label>
          <label>URL (JSON-RPC endpoint)<input name="url" placeholder="http://127.0.0.1:8770/mcp" required /></label>
        </div>
        <div className="cap-add-row">
          <label>Risk tier
            <select name="risk_tier" defaultValue="1">
              <option value="1">1 — auto</option>
              <option value="2">2 — needs approval</option>
              <option value="3">3 — high</option>
            </select>
          </label>
          <label>Data boundary
            <select name="data_boundary" defaultValue="internal">
              <option value="internal">internal</option>
              <option value="public">public</option>
              <option value="confidential">confidential</option>
              <option value="restricted">restricted</option>
              <option value="local-only">local-only</option>
            </select>
          </label>
          <label>Description<input name="description" placeholder="What this server provides" /></label>
        </div>
        <button type="submit" className="cap-btn primary">Register server</button>
      </form>
    </div>
  );
}
