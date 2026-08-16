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

      {/* Capability rules, Service gates and Budget gates lived here and are gone.
          All three were read-only restatements of configuration: a list of every tool by risk
          tier, the same gate settings shown elsewhere, and budget thresholds that the Budget
          area already owns. None of them could be acted on from this page, and together they
          pushed the one thing that DOES need a person -- the decision queue -- below the fold.
          The counts that mattered ("N auto, N need approval") are kept above. */}
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
