"""ONE RULES BOOK (BOSSAPP-572): every BOSS rule in one place, by category, with where each is
enforced, and role-scoped loading within a token budget.

Decision (2026-11-24):
  Build one read-only rules book generated from the code (rules by category, with where each is
  enforced), searchable in the console. Editing comes later.

Standing rule (2026-11-24):
  Agents act autonomously; never ask mid-build; no Community gating; restrict only customer data
  and resources; BOSS-side fixes get full control.

Every entry carries:
  - id          stable dot-delimited identifier
  - title       short scannable title
  - text        the rule statement
  - why         why this rule exists (so agents generalise rather than blindly recite)
  - category    autonomy | build | review | deploy | security | git | cost | ux | ops
  - roles       tuple of roles it applies to ("all" = universal short rulebook)
  - enforcement {kind: setting | gate | policy | prompt_only, ref: str, where: str}
  - source      platform_standard | project_setting | autonomy_policy | operator_rule |
                mandated_standard | role_policy
  - universal   True if part of the short rulebook handed to every agent
  - priority    ordering inside token budget (lower = higher priority)

Guard invariant (`verify_code_enforcement`):
  - Every rule with `enforcement.kind == "setting"` MUST map to a real knob in
    `project_settings.KNOBS` AND a real attribute on `config.Settings`.
  - Every rule with `enforcement.kind in ("gate", "policy")` MUST resolve to a real importable
    `module:symbol` in code.
  - Every rule with `enforcement.kind == "prompt_only"` MUST have `ref == ""` (never claim code
    enforcement it does not have).
"""
from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import text as _sql

CATEGORIES: tuple[str, ...] = (
    "autonomy",
    "build",
    "review",
    "deploy",
    "security",
    "git",
    "cost",
    "ux",
    "ops",
)

ENFORCEMENT_KINDS: tuple[str, ...] = ("setting", "gate", "policy", "prompt_only")

SOURCES: tuple[str, ...] = (
    "platform_standard",
    "project_setting",
    "autonomy_policy",
    "operator_rule",
    "mandated_standard",
    "role_policy",
)

#: Canonical mapping from agent role keys (and aliases) to the categories that role needs.
ROLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "coder": ("autonomy", "build", "git", "security", "cost"),
    "builder": ("autonomy", "build", "git", "security", "cost"),
    "forge": ("autonomy", "build", "git", "security", "cost"),
    "engineer": ("autonomy", "build", "git", "security", "cost"),
    "frontend": ("autonomy", "build", "ux", "git", "security"),
    "ux": ("autonomy", "ux", "review"),
    "designer": ("autonomy", "ux", "review"),
    "qa": ("autonomy", "build", "review", "security"),
    "tester": ("autonomy", "build", "review", "security"),
    "raptor": ("autonomy", "build", "review", "security"),
    "reviewer": ("autonomy", "review", "security", "git", "build"),
    "security": ("autonomy", "security", "review", "deploy"),
    "security_reviewer": ("autonomy", "security", "review", "deploy"),
    "deployer": ("autonomy", "deploy", "ops", "security"),
    "sre": ("autonomy", "deploy", "ops", "security", "cost"),
    "harbor": ("autonomy", "deploy", "ops", "security"),
    "infra_agent": ("autonomy", "deploy", "ops", "cost", "security"),
    "shepherd": ("autonomy", "ops", "cost"),
    "boss_maintainer": ("autonomy", "ops", "build", "review", "git", "security"),
    "mender": ("autonomy", "ops", "build", "review", "git", "security"),
    "engineering_captain": ("autonomy", "build", "review", "deploy", "cost", "ops", "ux"),
    "pm": ("autonomy", "cost", "ux", "ops"),
    "architect": ("autonomy", "build", "deploy", "security", "cost"),
    "finops": ("autonomy", "cost", "ops"),
}


@dataclass(frozen=True)
class RuleEntry:
    id: str
    title: str
    text: str
    why: str
    category: str
    roles: tuple[str, ...]
    enforcement_kind: str
    enforcement_ref: str
    enforcement_where: str
    source: str
    universal: bool = False
    priority: int = 50

    def as_dict(self, *, effective_value: Any = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "text": self.text,
            "why": self.why,
            "category": self.category,
            "roles": list(self.roles),
            "enforcement": {
                "kind": self.enforcement_kind,
                "ref": self.enforcement_ref,
                "where": self.enforcement_where,
            },
            "source": self.source,
            "universal": self.universal,
            "priority": self.priority,
        }
        if effective_value is not None:
            out["effective_value"] = effective_value
        return out


#: Canonical code-defined BOSS rules book (inventory of platform standards, project settings,
#: autonomy policy, code gates, and standing operator rules).
RULES_BOOK: tuple[RuleEntry, ...] = (
    # ── AUTONOMY ─────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="autonomy.dev_act",
        title="Agents act autonomously in development",
        text=(
            "Outside production, agents execute development work without interrupting a person "
            "for routine confirmations; production and unclassifiable remotes always ask."
        ),
        why=(
            "Repeated mid-build approvals stall local development while humans only need to "
            "validate production releases and final outputs (BOSSAPP-1441)."
        ),
        category="autonomy",
        roles=("all",),
        enforcement_kind="policy",
        enforcement_ref="boss_engine.services.autonomy_policy:DEFAULT",
        enforcement_where="engine/boss_engine/services/autonomy_policy.py · DEFAULT / load()",
        source="autonomy_policy",
        universal=True,
        priority=5,
    ),
    RuleEntry(
        id="autonomy.never_ask_mid_build",
        title="Never ask mid-build unless an explicit stop or production boundary applies",
        text=(
            "Finish the build, tests, and PR end-to-end without pausing to ask clarifying "
            "questions mid-build; honour only explicit human stops and production boundaries."
        ),
        why="Mid-build prompts leave runs parked waiting for an operator instead of delivering a reviewable diff.",
        category="autonomy",
        roles=("all",),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.autonomy_gate:decide",
        enforcement_where="engine/boss_engine/services/autonomy_gate.py · decide()",
        source="operator_rule",
        universal=True,
        priority=6,
    ),
    RuleEntry(
        id="autonomy.no_community_gating",
        title="No Community edition gating; restrict only customer data and resources",
        text=(
            "Do not gate BOSS capabilities behind Community vs Enterprise switches; restrict only "
            "customer data boundaries, tenant scopes, and customer infrastructure resources."
        ),
        why="Artificial edition gates break operator workflows without improving security.",
        category="autonomy",
        roles=("all",),
        enforcement_kind="prompt_only",
        enforcement_ref="",
        enforcement_where="Standing operator rule (2026-11-24)",
        source="operator_rule",
        universal=True,
        priority=8,
    ),
    RuleEntry(
        id="autonomy.boss_side_full_control",
        title="BOSS-side tooling and fixes get full control within governed PRs",
        text=(
            "For BOSS-side tooling, diagnostics, and self-fix proposals, agents have full read/test "
            "control and deliver changes as validated PRs or reviewed self-fixes."
        ),
        why="BOSS must be able to diagnose and propose fixes for its own platform defects cleanly.",
        category="autonomy",
        roles=("boss_maintainer", "mender", "engineering_captain", "coder", "sre"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.self_fix:TRANSITIONS",
        enforcement_where="engine/boss_engine/services/self_fix.py · TRANSITIONS",
        source="operator_rule",
        universal=False,
        priority=12,
    ),
    # ── SECURITY ─────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="security.never_print_credential",
        title="Never print, log, or commit any credential or secret",
        text=(
            "Never print, echo, embed in error messages, or commit API keys, tokens, passwords, "
            "or private keys; every output passes redaction before storage or display."
        ),
        why="A single printed secret in logs, PR diffs, or agent output leaks credentials permanently.",
        category="security",
        roles=("all",),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.redaction:redact_secrets",
        enforcement_where="engine/boss_engine/services/redaction.py · redact_secrets()",
        source="operator_rule",
        universal=True,
        priority=1,
    ),
    RuleEntry(
        id="security.tool_result_screen",
        title="Screen outside tool results for prompt injection",
        text=(
            "Tool outputs from untrusted external origins are screened for prompt injection; a hit "
            "marks the result untrusted and holds subsequent writes for human review."
        ),
        why="Untrusted payloads in repos, tickets, or web responses must not hijack agent actions.",
        category="security",
        roles=("coder", "builder", "reviewer", "security", "security_reviewer", "sre"),
        enforcement_kind="setting",
        enforcement_ref="tool_result_screen",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['tool_result_screen']",
        source="project_setting",
        universal=False,
        priority=10,
    ),
    RuleEntry(
        id="security.project_only_stricter",
        title="A project policy may only be stricter than its organization policy",
        text=(
            "Project-level overrides may tighten autonomy or top-up caps relative to the org "
            "default, never loosen them."
        ),
        why="Org-wide safety boundaries must hold across every project in the tenant.",
        category="security",
        roles=("security", "security_reviewer", "engineering_captain", "deployer"),
        enforcement_kind="policy",
        enforcement_ref="boss_engine.services.autonomy_policy:validate_stricter",
        enforcement_where="engine/boss_engine/services/autonomy_policy.py · validate_stricter()",
        source="autonomy_policy",
        universal=False,
        priority=15,
    ),
    RuleEntry(
        id="security.instructions_cannot_widen_tools",
        title="Agent instructions shape how an agent works, never what tools it may call",
        text=(
            "Operator instruction files and prompts cannot grant new tools, waive gates, or "
            "override L0 rules; permissions come strictly from role_skills and role_mcp_servers."
        ),
        why="Separating prose instructions from tool grants prevents privilege escalation via prompts.",
        category="security",
        roles=("security", "security_reviewer", "reviewer", "boss_maintainer"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.agent_instructions:lint_instructions",
        enforcement_where="engine/boss_engine/services/agent_instructions.py · lint_instructions()",
        source="role_policy",
        universal=False,
        priority=18,
    ),
    # ── BUILD ────────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="build.open_pr_when_checks_fail",
        title="Open the pull request even when checks fail",
        text=(
            "After repair rounds finish, open a draft pull request labelled `checks failing` "
            "listing every failing check and finding (security first) rather than discarding work."
        ),
        why="A visible PR lets humans inspect and finish partial progress instead of losing work.",
        category="build",
        roles=("coder", "builder", "forge", "engineer", "engineering_captain", "qa"),
        enforcement_kind="setting",
        enforcement_ref="open_pr_when_checks_fail",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['open_pr_when_checks_fail']",
        source="project_setting",
        universal=False,
        priority=12,
    ),
    RuleEntry(
        id="build.observability_metrics",
        title="Every network service exposes Prometheus /metrics and declares it in boss.deploy.yaml",
        text=(
            "Expose GET /metrics in Prometheus 0.0.4 text format with http_requests_total, "
            "http_request_duration_seconds, app_info, app_uptime_seconds, and domain outcome "
            "counters using templated route labels (never raw paths or PII)."
        ),
        why="Unbounded labels break Prometheus cardinality and undeclared /metrics endpoints leave deployed apps invisible.",
        category="build",
        roles=("coder", "builder", "forge", "engineer", "qa", "tester", "raptor", "architect"),
        enforcement_kind="policy",
        enforcement_ref="boss_engine.services.platform_standards:OBSERVABILITY_STANDARD",
        enforcement_where="engine/boss_engine/services/platform_standards.py · OBSERVABILITY_STANDARD",
        source="platform_standard",
        universal=False,
        priority=20,
    ),
    RuleEntry(
        id="build.dependency_integrity",
        title="Install exact lockfile dependencies without pruning optional platform binaries",
        text=(
            "Use frozen lockfile installs (`npm ci`) on the same base image libc family as runtime; "
            "never pass `--omit=optional` or `--no-optional` before building."
        ),
        why="Native build tools (rollup, esbuild, swc) ship as optional platform dependencies and fail inside images when pruned.",
        category="build",
        roles=("coder", "builder", "forge", "engineer", "deployer", "sre"),
        enforcement_kind="policy",
        enforcement_ref="boss_engine.services.platform_standards:DEPENDENCY_INTEGRITY_STANDARD",
        enforcement_where="engine/boss_engine/services/platform_standards.py · DEPENDENCY_INTEGRITY_STANDARD",
        source="platform_standard",
        universal=False,
        priority=22,
    ),
    RuleEntry(
        id="build.no_secrets_in_tests",
        title="Never write literal passwords, tokens, or keys in tests or fixtures",
        text=(
            "Generate test credentials at runtime (randomUUID / token_urlsafe) or read from test "
            "environment variables; never commit secret-looking literals in tests."
        ),
        why="BOSS's committed-secrets gate scans test files too and blocks repair rounds when QA writes literal secrets.",
        category="build",
        roles=("qa", "tester", "raptor", "coder", "builder", "forge", "engineer"),
        enforcement_kind="prompt_only",
        enforcement_ref="",
        enforcement_where="engine/boss_engine/services/governed_code_task_service.py · _NO_SECRETS_IN_TESTS",
        source="platform_standard",
        universal=False,
        priority=24,
    ),
    RuleEntry(
        id="build.quality_bar",
        title="Enforce the project's quality bar across build gates",
        text=(
            "Evaluate every build against its resolved quality bar (prototype, standard, or "
            "production); floor gates (build, start, own tests, security, secrets) always block."
        ),
        why="A single quality bar keeps build strictness predictable from prototype through production.",
        category="build",
        roles=("coder", "builder", "qa", "reviewer", "engineering_captain"),
        enforcement_kind="setting",
        enforcement_ref="quality_bar",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['quality_bar']",
        source="project_setting",
        universal=False,
        priority=16,
    ),
    # ── REVIEW ───────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="review.checks_failing_explicit_merge",
        title="Merging over red checks always requires an explicit human decision",
        text=(
            "A PR opened with failing checks is never auto-merged by dev autonomy; a person must "
            "explicitly review the failing checks and decide."
        ),
        why="Opening a PR with failing checks preserves visibility without ever silently shipping broken code.",
        category="review",
        roles=("reviewer", "security_reviewer", "qa", "engineering_captain"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.checks_failing:enabled",
        enforcement_where="engine/boss_engine/services/checks_failing.py · enabled() & autonomy_gate.py",
        source="role_policy",
        universal=False,
        priority=14,
    ),
    RuleEntry(
        id="review.quality_bar_resolution",
        title="Resolve per-gate blocking vs follow-up levels deterministically",
        text=(
            "Gate verdicts are resolved deterministically from project quality_bar and "
            "quality_bar_gates without weakening floor gates."
        ),
        why="Deterministic gate resolution prevents inconsistent review enforcement across runs.",
        category="review",
        roles=("reviewer", "qa", "engineering_captain"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.quality_bar:resolve_for_run",
        enforcement_where="engine/boss_engine/services/quality_bar.py · resolve_for_run()",
        source="role_policy",
        universal=False,
        priority=25,
    ),
    # ── DEPLOY ───────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="deploy.prod_always_asks",
        title="Production deploys and destructive provider writes always require approval",
        text=(
            "Every production deployment, DNS cutover, or destructive infrastructure write requires "
            "an explicit human approval and rollback plan; unknown remotes default to production."
        ),
        why="Autonomous dev speed must never risk unapproved production outages or data loss.",
        category="deploy",
        roles=("all",),
        enforcement_kind="policy",
        enforcement_ref="boss_engine.services.autonomy_policy:DEFAULT",
        enforcement_where="engine/boss_engine/services/autonomy_policy.py · DEFAULT.prod == 'ask'",
        source="autonomy_policy",
        universal=True,
        priority=4,
    ),
    RuleEntry(
        id="deploy.precheck_at_the_door",
        title="Run fast infrastructure pre-checks at the start door before spend",
        text=(
            "Every workflow capability declares its pre-checks (workflow_precheck.CHECKS) and "
            "validates consent, model connectivity, binaries, and budget headroom before starting."
        ),
        why="Catching broken infrastructure at pre-check prevents hours of wasted agent spend.",
        category="deploy",
        roles=("deployer", "sre", "harbor", "engineering_captain", "boss_maintainer"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.workflow_precheck:gate_start",
        enforcement_where="engine/boss_engine/services/workflow_precheck.py · gate_start()",
        source="role_policy",
        universal=False,
        priority=15,
    ),
    RuleEntry(
        id="deploy.fix_attempts",
        title="Bound automatic deploy-repair attempts per step",
        text=(
            "When a deploy fails due to the app, BOSS runs at most `deploy_fix_attempts` repair "
            "builds before escalating to a person."
        ),
        why="Bounded retries resolve transient or fixable app issues without infinite spend loops.",
        category="deploy",
        roles=("deployer", "sre", "harbor"),
        enforcement_kind="setting",
        enforcement_ref="deploy_fix_attempts",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['deploy_fix_attempts']",
        source="project_setting",
        universal=False,
        priority=26,
    ),
    # ── GIT ──────────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="git.branch_and_pr_only",
        title="Deliver changes on focused branches via pull requests — never self-modify directly",
        text=(
            "Propose all code and configuration changes on focused branches (`ai/codex/...` or "
            "run branches) as reviewable pull requests; never push directly to main or self-modify live code."
        ),
        why="Focused PRs guarantee auditability, CI verification, and human review before merge.",
        category="git",
        roles=("coder", "builder", "forge", "engineer", "boss_maintainer", "reviewer"),
        enforcement_kind="prompt_only",
        enforcement_ref="",
        enforcement_where="AGENTS.md · Hard rules & Agent Coordination",
        source="operator_rule",
        universal=False,
        priority=18,
    ),
    # ── COST ─────────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="cost.budget_reservation_required",
        title="Every spendy action is preceded by a BudgetReservation",
        text=(
            "Reserve budget before invoking metered models, tools, or paid infrastructure; release "
            "unused reservation headroom immediately when work finishes or fails."
        ),
        why="Up-front reservations prevent concurrent runs from overspending daily or monthly caps.",
        category="cost",
        roles=("all",),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.budget:BudgetGovernor",
        enforcement_where="engine/boss_engine/services/budget.py · BudgetGovernor",
        source="role_policy",
        universal=True,
        priority=3,
    ),
    RuleEntry(
        id="cost.budget_at_limit",
        title="Honour the project's at-limit budget behavior (pause, cheaper, or stop)",
        text=(
            "When a spend limit is reached, follow `budget_at_limit`: pause and ask, shift to "
            "cheaper models, or stop until the period resets."
        ),
        why="Predictable cap enforcement prevents surprise cloud or model bills.",
        category="cost",
        roles=("finops", "engineering_captain", "coder", "sre", "pm"),
        enforcement_kind="setting",
        enforcement_ref="budget_at_limit",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['budget_at_limit']",
        source="project_setting",
        universal=False,
        priority=14,
    ),
    RuleEntry(
        id="cost.unpriced_policy",
        title="Govern spend BOSS cannot price (estimate vs block)",
        text=(
            "Handle unpriced model calls according to `budget_unpriced_policy` and its daily "
            "allowance so unknown pricing never silently bypasses spending limits."
        ),
        why="Unpriced models could otherwise evade metered budget caps.",
        category="cost",
        roles=("finops", "engineering_captain", "sre"),
        enforcement_kind="setting",
        enforcement_ref="budget_unpriced_policy",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['budget_unpriced_policy']",
        source="project_setting",
        universal=False,
        priority=28,
    ),
    RuleEntry(
        id="cost.call_classes",
        title="Apply per-class volume and allowance caps across model calls",
        text=(
            "Enforce per-class allow/deny, daily call, and daily token limits configured in "
            "`call_classes` across build_stage, chat, panel, decision, proof, kg, and gateway calls."
        ),
        why="Volume caps protect subscription plans where dollar caps read as $0.",
        category="cost",
        roles=("finops", "engineering_captain", "infra_agent"),
        enforcement_kind="setting",
        enforcement_ref="call_classes",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['call_classes']",
        source="project_setting",
        universal=False,
        priority=30,
    ),
    # ── OPS ──────────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="ops.fix_class_not_instance",
        title="Fix the failure class, never only the single instance",
        text=(
            "When diagnosing a bug or run failure, identify and fix the underlying class of "
            "failure with a regression test rather than patching one symptom."
        ),
        why="Instance patches leave the next run vulnerable to the exact same root cause.",
        category="ops",
        roles=("all",),
        enforcement_kind="prompt_only",
        enforcement_ref="",
        enforcement_where="Standing operator rule (operator memory → BOSS rules book)",
        source="operator_rule",
        universal=True,
        priority=7,
    ),
    RuleEntry(
        id="ops.every_failure_has_owner",
        title="Every failure is visible and owned — never silently dropped",
        text=(
            "Every failed build, workflow run, deploy, or job is either actively being fixed by "
            "an agent, escalated to a person with a clear action item, or resolved."
        ),
        why="Silent drops leave broken work invisible in queues and dead-letter tables.",
        category="ops",
        roles=("shepherd", "sre", "infra_agent", "boss_maintainer", "engineering_captain"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.failure_owner:sweep",
        enforcement_where="engine/boss_engine/services/failure_owner.py · sweep()",
        source="role_policy",
        universal=False,
        priority=10,
    ),
    RuleEntry(
        id="ops.harness_failure_stops_blind_work",
        title="Stop immediately on harness/sandbox failures rather than working blind",
        text=(
            "When a streaming engine reports a broken sandbox, missing binary, or refused login, "
            "latch and halt the run on the first turn so the agent does not burn turns blind."
        ),
        why="Agents cannot fix missing host binaries or broken sandboxes by editing app code.",
        category="ops",
        roles=("coder", "builder", "sre", "shepherd", "boss_maintainer"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.harness_failures:check_turn",
        enforcement_where="engine/boss_engine/services/harness_failures.py · check_turn()",
        source="role_policy",
        universal=False,
        priority=11,
    ),
    RuleEntry(
        id="ops.failure_proposes_precheck",
        title="Every failed run classifies its cause and proposes a pre-check",
        text=(
            "When a run fails, classify its cause and persist a deduplicated pre-check proposal "
            "linked to issue reporting; never auto-enable proposed checks without human approval."
        ),
        why="The pre-check suite should grow systematically from real failures rather than guesses (BOSSAPP-541).",
        category="ops",
        roles=("shepherd", "boss_maintainer", "mender", "sre", "engineering_captain"),
        enforcement_kind="gate",
        enforcement_ref="boss_engine.services.precheck_proposals:record_failure_proposal",
        enforcement_where="engine/boss_engine/services/precheck_proposals.py · record_failure_proposal()",
        source="role_policy",
        universal=False,
        priority=13,
    ),
    RuleEntry(
        id="ops.step_recovery",
        title="Allow bounded agent step recovery when enabled",
        text=(
            "When `step_recovery` is enabled, a failed build or deploy step's agent inspects the "
            "evidence and retries, repairs, or escalates within its recovery time and spend caps."
        ),
        why="Automated recovery resolves common build/deploy glitches without waking an operator.",
        category="ops",
        roles=("sre", "deployer", "harbor", "shepherd"),
        enforcement_kind="setting",
        enforcement_ref="step_recovery",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['step_recovery']",
        source="project_setting",
        universal=False,
        priority=24,
    ),
    RuleEntry(
        id="ops.agents_fix_failures_with_starter_authority",
        title="Agents repair failed runs under the starter's bounded authority",
        text=(
            "When `agents_fix_failures` is enabled, agents may repair work started by a person "
            "under their authority (excluding merges, prod deploys, secrets, and approvals)."
        ),
        why="Avoids forcing the starter to re-grant identical permissions on every automated retry.",
        category="ops",
        roles=("shepherd", "sre", "engineering_captain"),
        enforcement_kind="setting",
        enforcement_ref="agents_fix_failures",
        enforcement_where="engine/boss_engine/services/project_settings.py · KNOBS['agents_fix_failures']",
        source="project_setting",
        universal=False,
        priority=26,
    ),
    # ── UX ───────────────────────────────────────────────────────────────────────────────────
    RuleEntry(
        id="ux.tokens_and_copy_budget",
        title="Use design-system tokens only and concise operator copy",
        text=(
            "Console surfaces use CSS custom property tokens (no inline hex colors, type from "
            "--fs-*), respect container responsiveness (1440px and 390px), and keep UI chrome concise."
        ),
        why="Consistent tokens and crisp copy prevent visual drift and mobile layout regressions.",
        category="ux",
        roles=("frontend", "ux", "designer", "engineering_captain", "pm"),
        enforcement_kind="prompt_only",
        enforcement_ref="",
        enforcement_where="apps/web/tests/events-are-tokens-only.test.ts & activity-govern-copy-budget.test.tsx",
        source="role_policy",
        universal=False,
        priority=22,
    ),
)


def estimate_tokens(text: str) -> int:
    """Deterministic ~4 chars/token estimate for prompt budget enforcement."""
    clean = str(text or "")
    return max(1, math.ceil(len(clean) / 4)) if clean else 0


def _mandated_entries(connection: Any, project_id: Optional[str]) -> list[dict[str, Any]]:
    if connection is None or not project_id:
        return []
    point = connection.begin_nested() if hasattr(connection, "begin_nested") else None
    try:
        rows = connection.execute(
            _sql(
                "SELECT standard_key, title, body, scope, mandated_by_role, rationale "
                "FROM engineering_standards "
                "WHERE status = 'active' AND (project_id IS NULL OR project_id = :p) "
                "ORDER BY project_id NULLS FIRST, created_at"
            ),
            {"p": project_id},
        ).mappings().all()
        if point is not None:
            point.commit()
    except Exception:  # noqa: BLE001 — missing table should never break the rules book
        if point is not None:
            point.rollback()
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["standard_key"] or "").strip()
        role_by = str(row.get("mandated_by_role") or "workforce")
        out.append(
            {
                "id": f"mandated.{key}",
                "title": str(row["title"] or key),
                "text": str(row["body"] or "").strip(),
                "why": str(row.get("rationale") or f"Mandated by {role_by} for scope '{row.get('scope') or 'service'}'."),
                "category": "build",
                "roles": ["all"] if row.get("scope") == "all" else ["coder", "builder", "qa", "reviewer"],
                "enforcement": {
                    "kind": "prompt_only",
                    "ref": "",
                    "where": f"engineering_standards · mandated by {role_by}",
                },
                "source": "mandated_standard",
                "universal": row.get("scope") == "all",
                "priority": 35,
            }
        )
    return out


def _effective_settings(connection: Any, project_id: Optional[str]) -> dict[str, Any]:
    if connection is None or not project_id:
        return {}
    point = connection.begin_nested() if hasattr(connection, "begin_nested") else None
    try:
        from . import project_settings

        rows = connection.execute(
            _sql("SELECT key, value FROM project_settings WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().all()
        if point is not None:
            point.commit()
        return {str(r["key"]): r["value"] for r in rows if str(r["key"]) in project_settings.KNOBS}
    except Exception:  # noqa: BLE001
        if point is not None:
            point.rollback()
        return {}


def _live_autonomy_summary(connection: Any, project_id: Optional[str]) -> Optional[str]:
    if connection is None or not project_id:
        return None
    try:
        from . import autonomy_policy

        pol = autonomy_policy.load(connection, project_id)
        return autonomy_policy.describe(pol)["line"]
    except Exception:  # noqa: BLE001
        return None


def _role_matches(rule_roles: list[str], role_key: str, category: str) -> bool:
    norm = (role_key or "").strip().lower()
    if not norm:
        return True
    lowered = {r.lower() for r in rule_roles}
    if "all" in lowered or norm in lowered:
        return True
    allowed_cats = ROLE_CATEGORIES.get(norm)
    if allowed_cats and category in allowed_cats:
        return True
    return False


def list_rules(
    *,
    connection: Any = None,
    project_id: Optional[str] = None,
    category: Optional[str] = None,
    role: Optional[str] = None,
    enforcement_kind: Optional[str] = None,
    q: Optional[str] = None,
) -> dict[str, Any]:
    """Return the unified rules book, optionally enriched from project state and filtered."""
    overrides = _effective_settings(connection, project_id)
    autonomy_line = _live_autonomy_summary(connection, project_id)

    all_items: list[dict[str, Any]] = []
    for entry in RULES_BOOK:
        eff: Any = None
        if entry.enforcement_kind == "setting" and entry.enforcement_ref in overrides:
            eff = overrides[entry.enforcement_ref]
        elif entry.id == "autonomy.dev_act" and autonomy_line:
            eff = autonomy_line
        all_items.append(entry.as_dict(effective_value=eff))
    all_items.extend(_mandated_entries(connection, project_id))

    filtered: list[dict[str, Any]] = []
    cat_filter = (category or "").strip().lower()
    role_filter = (role or "").strip().lower()
    enf_filter = (enforcement_kind or "").strip().lower()
    query = (q or "").strip().lower()

    for item in all_items:
        if cat_filter and item["category"].lower() != cat_filter:
            continue
        if enf_filter and item["enforcement"]["kind"].lower() != enf_filter:
            continue
        if role_filter and not _role_matches(item["roles"], role_filter, item["category"]):
            continue
        if query:
            hay = " ".join([
                item["id"],
                item["title"],
                item["text"],
                item["why"],
                item["category"],
                item["source"],
                item["enforcement"]["kind"],
                item["enforcement"]["where"],
                " ".join(item["roles"]),
            ]).lower()
            if query not in hay:
                continue
        filtered.append(item)

    filtered.sort(key=lambda r: (CATEGORIES.index(r["category"]) if r["category"] in CATEGORIES else 99, r["priority"], r["id"]))

    counts_by_category: dict[str, int] = {c: 0 for c in CATEGORIES}
    counts_by_enforcement: dict[str, int] = {k: 0 for k in ENFORCEMENT_KINDS}
    for item in filtered:
        counts_by_category[item["category"]] = counts_by_category.get(item["category"], 0) + 1
        ek = item["enforcement"]["kind"]
        counts_by_enforcement[ek] = counts_by_enforcement.get(ek, 0) + 1

    return {
        "rules": filtered,
        "categories": list(CATEGORIES),
        "enforcement_kinds": list(ENFORCEMENT_KINDS),
        "roles": sorted(ROLE_CATEGORIES.keys()),
        "counts_by_category": counts_by_category,
        "counts_by_enforcement": counts_by_enforcement,
        "total": len(filtered),
        "read_only": True,
    }


def _format_rule_line(rule: Mapping[str, Any]) -> str:
    enf = rule.get("enforcement") or {}
    kind = enf.get("kind") or "prompt_only"
    badge = f"[{rule['category']}/{kind}]"
    return f"- {rule['id']} {badge}: {rule['text']} (Why: {rule['why']})"


def rules_for_role(
    role_key: str,
    *,
    connection: Any = None,
    project_id: Optional[str] = None,
    max_tokens: int = 1200,
    include_universal: bool = True,
) -> dict[str, Any]:
    """Load only the universal short rulebook + categories relevant to `role_key`, within budget."""
    norm = (role_key or "").strip().lower() or "coder"
    budget = max(64, int(max_tokens or 1200))
    book = list_rules(connection=connection, project_id=project_id)["rules"]
    cats = list(ROLE_CATEGORIES.get(norm, ("autonomy", "security", "ops")))

    applicable: list[dict[str, Any]] = []
    for item in book:
        is_univ = bool(item.get("universal") or "all" in item.get("roles", []))
        if is_univ:
            if include_universal:
                applicable.append(item)
            continue
        if _role_matches(item["roles"], norm, item["category"]):
            applicable.append(item)

    # Universal rules first, then explicitly role-targeted rules, then code-enforced, then priority.
    def _sort_key(r: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
        roles_lower = {str(x).lower() for x in r.get("roles", [])}
        is_univ = 0 if (r.get("universal") or "all" in roles_lower) else 1
        direct_role = 0 if norm in roles_lower else 1
        enforced_rank = 0 if (r.get("enforcement") or {}).get("kind") != "prompt_only" else 1
        return (is_univ, direct_role, enforced_rank, int(r.get("priority", 50)), str(r["id"]))

    applicable.sort(key=_sort_key)

    header = f"=== BOSS RULES BOOK (role: {norm}) ==="
    footer = "=== END BOSS RULES BOOK ==="
    tokens_used = estimate_tokens(header + "\n" + footer)
    included: list[dict[str, Any]] = []
    lines: list[str] = [header]

    for item in applicable:
        line = _format_rule_line(item)
        cost = estimate_tokens(line + "\n")
        if included and tokens_used + cost > budget:
            continue
        if not included and tokens_used + cost > budget:
            # Always fit at least the highest-priority universal rule (truncated if tiny budget).
            allowed_chars = max(40, (budget - tokens_used) * 4)
            line = line[:allowed_chars].rstrip()
            cost = estimate_tokens(line + "\n")
        included.append(item)
        lines.append(line)
        tokens_used += cost

    lines.append(footer)
    rendered = "\n".join(lines)
    omitted = len(applicable) - len(included)
    return {
        "role": norm,
        "categories": cats,
        "rules": included,
        "text": rendered,
        "tokens_used": estimate_tokens(rendered),
        "tokens_budget": budget,
        "truncated": omitted > 0,
        "omitted_count": omitted,
    }


def format_role_rulebook(
    role_key: str,
    *,
    connection: Any = None,
    project_id: Optional[str] = None,
    max_tokens: int = 1200,
) -> str:
    """Render the role-scoped rulebook block ready for system prompt inclusion."""
    return str(
        rules_for_role(
            role_key,
            connection=connection,
            project_id=project_id,
            max_tokens=max_tokens,
        )["text"]
    )


def verify_code_enforcement() -> list[str]:
    """Verify every code-enforced rule in RULES_BOOK maps to a real setting or gate in code.

    Returns a list of violation descriptions (empty list = all rules honest and verified).
    """
    from ..config import Settings
    from . import project_settings

    violations: list[str] = []
    seen_ids: set[str] = set()
    settings_fields = set(Settings.model_fields.keys())

    for entry in RULES_BOOK:
        if entry.id in seen_ids:
            violations.append(f"duplicate rule id: {entry.id}")
        seen_ids.add(entry.id)
        if entry.category not in CATEGORIES:
            violations.append(f"{entry.id}: unknown category '{entry.category}'")
        if entry.enforcement_kind not in ENFORCEMENT_KINDS:
            violations.append(f"{entry.id}: unknown enforcement kind '{entry.enforcement_kind}'")
        if entry.source not in SOURCES:
            violations.append(f"{entry.id}: unknown source '{entry.source}'")
        if not entry.text.strip() or not entry.why.strip():
            violations.append(f"{entry.id}: text and why are required")

        if entry.enforcement_kind == "prompt_only":
            if entry.enforcement_ref:
                violations.append(
                    f"{entry.id}: prompt_only rule must not claim code ref '{entry.enforcement_ref}'"
                )
            continue

        if entry.enforcement_kind == "setting":
            knob = project_settings.KNOBS.get(entry.enforcement_ref)
            if knob is None:
                violations.append(
                    f"{entry.id}: setting '{entry.enforcement_ref}' not found in project_settings.KNOBS"
                )
            else:
                attr = knob.get("attr")
                if attr not in settings_fields:
                    violations.append(
                        f"{entry.id}: setting attr '{attr}' not found on config.Settings"
                    )
            continue

        # gate or policy: must resolve to a real module:symbol
        if ":" not in entry.enforcement_ref:
            violations.append(
                f"{entry.id}: {entry.enforcement_kind} ref '{entry.enforcement_ref}' must be 'module:symbol'"
            )
            continue
        mod_name, sym_name = entry.enforcement_ref.split(":", 1)
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001
            violations.append(f"{entry.id}: cannot import '{mod_name}': {exc}")
            continue
        if not hasattr(mod, sym_name):
            violations.append(f"{entry.id}: symbol '{sym_name}' not found in '{mod_name}'")

    return violations


__all__ = [
    "CATEGORIES",
    "ENFORCEMENT_KINDS",
    "ROLE_CATEGORIES",
    "RULES_BOOK",
    "RuleEntry",
    "SOURCES",
    "estimate_tokens",
    "format_role_rulebook",
    "list_rules",
    "rules_for_role",
    "verify_code_enforcement",
]
