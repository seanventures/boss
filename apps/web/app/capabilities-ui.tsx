import type { NativeTool, McpServerWithTools, SkillSummary } from "../lib/capabilities";

function riskLabel(tier: number): { label: string; tone: string } {
  if (tier <= 0) return { label: "read-only", tone: "muted" };
  if (tier === 1) return { label: "tier 1, auto", tone: "blue" };
  return { label: `tier ${tier}, approval`, tone: "amber" };
}

export function CapabilitiesView({
  projectId,
  actor = "executive_console",
  nativeTools,
  skills,
  mcpServers,
  apiReachable,
  engineLabel,
  engineModel,
}: {
  projectId: string;
  /** Who is signed in, so a server change is recorded against them. */
  actor?: string;
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
          <span className="cap-engine-note">BOSS drives the loop, this backend serves each completion</span>
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
        <span className="cap-section-sub">Built-in, governed by risk tier</span>
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
        <span className="cap-section-sub">Governed capability units the execution plane runs, shared across agents</span>
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

      {/* ---- MCP servers ----
          THE SAME PANEL the Skills and MCP page mounts. This screen had its own list and its
          own form, built on server actions that threw on any refusal, a bad URL or a taken
          name showed the generic error page, had no tier 0, called tier 1 "auto", could not
          edit a server and removed one with no question asked while steps still used it. */}
      <div className="cap-section-head">
        <h3>MCP servers</h3>
        <span className="cap-section-sub">Pluggable tool backends, JSON-RPC over HTTP, plug in external &amp; org-specific servers</span>
      </div>
      {!apiReachable ? (
        <p className="detail">Capabilities API unreachable.</p>
      ) : (
        // ONE HOME FOR MCP SERVERS: Skills & MCP › Library manages them; this links there.
        <a className="kit-link" href="?area=skills&tab=library&lib=mcp" data-testid="capabilities-mcp-link">
          MCP servers, {(mcpServers as Array<{ enabled?: boolean; reachable?: boolean | null; trusted?: boolean }>)
            .filter((s) => s.enabled !== false && s.reachable === true && s.trusted).length} ready ↗
        </a>
      )}
    </div>
  );
}
