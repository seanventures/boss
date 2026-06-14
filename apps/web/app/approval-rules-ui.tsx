import type {
  ApprovalRulesResponse,
  ApprovalToolRule,
  ApprovalServiceGate,
} from "../lib/approval-rules";

function fmtUsd(micros?: number): string {
  if (!micros && micros !== 0) return "—";
  return `$${(micros / 1e6).toFixed(2)}`;
}
function fmtPct(p?: number): string {
  if (p === undefined || p === null) return "—";
  return `${Math.round(p * 100)}%`;
}

export function ApprovalRulesView({
  rules,
  apiReachable,
}: {
  rules?: ApprovalRulesResponse;
  apiReachable: boolean;
}) {
  if (!apiReachable || !rules) {
    return <p className="detail">Approval rules unavailable.</p>;
  }
  const needs = rules.tool_rules.filter((t) => t.requires_approval);
  const auto = rules.tool_rules.filter((t) => !t.requires_approval);
  const sorted = [...rules.tool_rules].sort(
    (a, b) => (b.requires_approval ? 1 : 0) - (a.requires_approval ? 1 : 0) || b.risk_tier - a.risk_tier,
  );

  // group service gates by domain (preserve first-seen order)
  const byDomain = new Map<string, ApprovalServiceGate[]>();
  for (const g of rules.service_gates) {
    const arr = byDomain.get(g.domain) ?? [];
    arr.push(g);
    byDomain.set(g.domain, arr);
  }

  const bg = rules.budget_gates ?? {};

  return (
    <div className="ar">
      <p className="cap-intro">
        These are the rules that determine what BOSS must bring to you for approval before it takes a real
        effect. Anything that clears them runs autonomously; anything that doesn&apos;t becomes a Decision in
        the inbox below.
      </p>

      {/* Autonomy threshold */}
      <div className="ar-threshold">
        <span className="ar-threshold-badge">tier ≥ {rules.autonomy_threshold + 1}</span>
        <div className="ar-threshold-body">
          <strong>Autonomy threshold</strong>
          <p>{rules.autonomy_explainer}</p>
        </div>
        <div className="ar-threshold-stats">
          <span><b>{auto.length}</b> auto</span>
          <span><b>{needs.length}</b> need approval</span>
        </div>
      </div>

      {/* Capability rules */}
      <div className="cap-section-head">
        <h3>Capability rules</h3>
        <span className="cap-section-sub">Every tool, skill &amp; MCP server by risk tier → auto or needs-approval</span>
      </div>
      <div className="ar-grid">
        {sorted.map((t: ApprovalToolRule) => (
          <div key={t.source + t.name} className="ar-rule">
            <span className={`cap-badge tone-${t.requires_approval ? "amber" : "green"}`}>
              {t.requires_approval ? "approval" : "auto"}
            </span>
            <code className="ar-rule-name">{t.name}</code>
            <span className="ar-rule-meta">{t.source} · risk {t.risk_tier}</span>
          </div>
        ))}
      </div>

      {/* Service gates */}
      <div className="cap-section-head">
        <h3>Service gates</h3>
        <span className="cap-section-sub">Named actions that always route through approval, by domain</span>
      </div>
      <div className="ar-gates">
        {[...byDomain.entries()].map(([domain, gates]) => (
          <div key={domain} className="ar-gate-domain">
            <div className="ar-gate-domain-name">{domain}</div>
            {gates.map((g) => (
              <div key={g.gate} className="ar-gate">
                <span className="ar-gate-name">{g.gate}</span>
                <span className="ar-gate-trigger">{g.trigger}</span>
                <code className="ar-gate-reason">{g.reason}</code>
              </div>
            ))}
          </div>
        ))}
      </div>

      {/* Budget gates */}
      <div className="cap-section-head">
        <h3>Budget gates</h3>
        <span className="cap-section-sub">Spend thresholds that throttle or freeze actions for this org</span>
      </div>
      <div className="ar-budget">
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtUsd(bg.daily_cap_usd_micros)}</span><span>daily cap</span></div>
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtUsd(bg.monthly_cap_usd_micros)}</span><span>monthly cap</span></div>
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtPct(bg.soft_cap_pct)}</span><span>soft cap → warn</span></div>
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtPct(bg.auto_lite_mode_pct)}</span><span>→ lite mode</span></div>
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtPct(bg.freeze_provisioning_pct)}</span><span>→ freeze provisioning</span></div>
        <div className="ar-budget-item"><span className="ar-budget-n">{fmtPct(bg.freeze_tier2_pct)}</span><span>→ freeze tier-2</span></div>
      </div>

      {/* Policy records */}
      <div className="cap-section-head">
        <h3>Policy records</h3>
        <span className="cap-section-sub">Explicit governance policies stored for this org</span>
      </div>
      {rules.policies.length === 0 ? (
        <p className="detail">No explicit policy records for this org — governance is currently driven by the risk-tier threshold and service gates above.</p>
      ) : (
        <div className="ar-grid">
          {rules.policies.map((p) => (
            <div key={p.policy_key + p.version} className="ar-rule">
              <span className={`cap-badge tone-${p.status === "active" ? "green" : "muted"}`}>{p.status}</span>
              <code className="ar-rule-name">{p.policy_key}</code>
              <span className="ar-rule-meta">{p.title} · v{p.version}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
