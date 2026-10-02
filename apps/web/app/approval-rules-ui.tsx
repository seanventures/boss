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
        effect. Anything that clears them runs autonomously; anything that doesn&apos;t becomes a card in
        the approvals queue above.
      </p>

      {/* Autonomy threshold */}
      <div className="ar-threshold">
        <span className="ar-threshold-badge">tier ≥ {rules.autonomy_threshold + 1}</span>
        <div className="ar-threshold-body">
          <strong>Autonomy threshold</strong>
          <p>{rules.autonomy_explainer}</p>
        </div>
        {/* SAY WHAT IS BEING COUNTED.
            These count CAPABILITIES -- the tools, skills and MCP servers BOSS can invoke --
            not decisions. Sitting unlabelled above a decision queue, "42 need approval" reads
            as forty-two things waiting on you, when nothing is waiting at all. It got worse
            when the rule grid below was removed: the numbers lost the only thing that
            explained them, so the list comes back as an on-demand summary rather than a wall. */}
        <div className="ar-threshold-stats">
          <span><b>{auto.length}</b> of {rules.tool_rules.length} capabilities run on their own</span>
          <span><b>{needs.length}</b> need approval before they take effect</span>
        </div>
      </div>

      {/* BOTH SIDES OF THE RULE.
          Listing only what needs approval answers half the question: the useful thing to know
          about an autonomous system is as much what it does WITHOUT asking as what it stops
          for. Same disclosure, two groups, so the 80 capabilities reconcile against the two
          numbers above instead of having to be taken on trust. */}
      <details className="ar-capability-list">
        <summary>
          What BOSS may do — {needs.length} need approval, {auto.length} run automatically
        </summary>
        <p className="detail" style={{ margin: "6px 0" }}>
          Set by risk tier: anything at tier {rules.autonomy_threshold + 1} or above stops for a
          human. These are the rules, not pending requests.
        </p>

        <div className="cap-section-head" style={{ marginTop: 10 }}>
          <h4 style={{ margin: 0, fontSize: "0.82rem" }}>Needs approval ({needs.length})</h4>
          <span className="cap-section-sub">stops and becomes a Decision below</span>
        </div>
        <div className="ar-grid">
          {needs.map((t: ApprovalToolRule) => (
            <div key={t.source + t.name} className="ar-rule">
              <span className="cap-badge tone-amber">tier {t.risk_tier}</span>
              <code className="ar-rule-name">{t.name}</code>
              <span className="ar-rule-meta">{t.source}</span>
            </div>
          ))}
        </div>

        <div className="cap-section-head" style={{ marginTop: 14 }}>
          <h4 style={{ margin: 0, fontSize: "0.82rem" }}>Runs automatically ({auto.length})</h4>
          <span className="cap-section-sub">no approval — BOSS does these on its own</span>
        </div>
        <div className="ar-grid">
          {auto.map((t: ApprovalToolRule) => (
            <div key={t.source + t.name} className="ar-rule">
              <span className="cap-badge tone-green">tier {t.risk_tier}</span>
              <code className="ar-rule-name">{t.name}</code>
              <span className="ar-rule-meta">{t.source}</span>
            </div>
          ))}
        </div>
      </details>

      {/* REFERENCE, BELOW THE DECISIONS.
          Service gates and budget gates are worth keeping -- they say what BOSS will stop for
          -- but nothing here can be acted on, and above the queue they pushed the one thing
          that needs a person off the screen. Kept, moved, and labelled for what they are. */}
      <div className="ar-reference">
        <div className="cap-section-head">
          <h3>For reference — nothing here needs an action</h3>
          <span className="cap-section-sub">
            The gates BOSS already applies. Budgets are set in Govern.
          </span>
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

      </div>

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
