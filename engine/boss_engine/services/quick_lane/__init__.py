"""Quick Lane: small fixes in minutes through one warm agent session and its own queue.

Implements BOSSAPP-795..799 (docs/product/QUICK_LANE.md §3.1–3.6):
- QL-1 (queue.py): dedicated per-host queue (1 slot/host), person-first priority, memory-aware admission.
- QL-2 (context.py): full context in (Study notes, KG, learnings, scoped standards, request evidence)
  and strict reproduce-first enforcement before edits.
- QL-3 (validator.py): independent validation by a different agent/model (review + independent test)
  with bounded warm-session revision rounds and decision-ladder escalation.
- QL-4 (limits.py): 20-minute wall-clock, cost, diff, protected-path, and blast-radius caps,
  lossless hand-off to the full workflow, PR evidence builder, and bounded CI-after-open reopen.
- QL-5 (service.py): single governed entry point shared by Ask BOSS, console Quick fix, MCP
  create_code_task(lane="quick"), and ticket label boss:quick.
"""
from __future__ import annotations

from .context import (
    ContextBundle,
    ReproductionRecord,
    ReproductionRequiredError,
    load_full_context,
    render_context_prompt,
    validate_reproduction,
)
from .limits import (
    DEFAULT_COST_CAP_USD_MICROS,
    MAX_BLAST_RADIUS_CALLERS,
    MAX_CI_REOPENS,
    MAX_WALL_CLOCK_SECONDS,
    EscalationHandoff,
    QuickLimitCheck,
    build_pr_evidence,
    check_quick_limits,
    escalate_to_full_workflow,
    handle_ci_result,
    render_pr_body,
)
from .queue import (
    DEFAULT_REQUIRED_MEMORY_MB,
    JOB_KIND,
    MAX_CONCURRENT_PER_HOST,
    WORKLOAD_CLASS,
    AdmitDecision,
    admit_quick_job,
    check_host_memory,
    claim_next_quick_job,
    host_active_quick_count,
    read_host_available_memory_mb,
)
from .service import (
    QUICK_LANE_LABEL,
    execute_quick_job,
    get_quick_run,
    lane_from_labels,
    list_quick_runs,
    start_quick_fix,
    suggest_quick_fix_offer,
    triage_quick_lane,
)
from .validator import (
    MAX_VALIDATION_ROUNDS,
    IndependentTestResult,
    ValidationFinding,
    ValidationVerdict,
    run_validation_loop,
    select_validator_model,
    validate_quick_change,
)

__all__ = [
    "AdmitDecision",
    "ContextBundle",
    "DEFAULT_COST_CAP_USD_MICROS",
    "DEFAULT_REQUIRED_MEMORY_MB",
    "EscalationHandoff",
    "IndependentTestResult",
    "JOB_KIND",
    "MAX_BLAST_RADIUS_CALLERS",
    "MAX_CI_REOPENS",
    "MAX_CONCURRENT_PER_HOST",
    "MAX_VALIDATION_ROUNDS",
    "MAX_WALL_CLOCK_SECONDS",
    "QUICK_LANE_LABEL",
    "QuickLimitCheck",
    "ReproductionRecord",
    "ReproductionRequiredError",
    "ValidationFinding",
    "ValidationVerdict",
    "WORKLOAD_CLASS",
    "admit_quick_job",
    "build_pr_evidence",
    "check_host_memory",
    "check_quick_limits",
    "claim_next_quick_job",
    "escalate_to_full_workflow",
    "execute_quick_job",
    "get_quick_run",
    "handle_ci_result",
    "host_active_quick_count",
    "lane_from_labels",
    "list_quick_runs",
    "load_full_context",
    "read_host_available_memory_mb",
    "render_context_prompt",
    "render_pr_body",
    "run_validation_loop",
    "select_validator_model",
    "start_quick_fix",
    "suggest_quick_fix_offer",
    "triage_quick_lane",
    "validate_quick_change",
    "validate_reproduction",
]
