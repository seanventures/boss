"""Learning while executing (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.1, §4.3, §4.4).

Learning is written AT THE MOMENT THE AGENT LEARNS IT, inside the one agent pipeline — never
reconstructed afterwards from the repository or a run summary. Two write paths share
``record()``, so dedupe, scrub, provenance and the live event behave the same for both:

1. the agent says it: the ``record_learning`` tool (``knowledge.propose`` bundle) calls
   ``record()``;
2. the pipeline sees it: stage-end hooks (build stage boundary and repair checkpoint, the engine
   turn stream, workflow step completion, ``AgentService.decide``) call ``write_back()`` with
   what they observed, with no extra model call.

Phase 0 freezes the signatures and the vocabulary; stream S2b implements them.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sqlalchemy.engine import Connection

LEARNING_KINDS = frozenset({
    "investigation", "test_failure", "fix", "workaround", "review_finding",
    "decision", "root_cause", "precedent", "fact",
})

# The kind-specific ``detail`` fields (§2.3). A trailing ``?`` marks an optional field.
DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "investigation": ("tried", "ruled_out", "found"),
    "test_failure": ("test", "error_signature", "cause"),
    "fix": ("change_summary", "files", "pr_url?"),
    "workaround": ("problem", "workaround", "permanent_fix?"),
    "decision": ("question", "chosen", "reasons", "alternatives"),
    "precedent": ("symptom", "cause_summary", "fix_summary", "applies_to", "reuse_count"),
}

# The hooks that call write_back() (§4.1, table "The pipeline sees it").
HOOKS = frozenset({"build_stage", "repair_checkpoint", "engine_turn", "step_complete", "agent_decide"})

# Confidence is capped by evidence (§4.3): none refuses, a log excerpt alone caps at 0.6, a
# passing test at 0.9; below 0.4 is stored but never injected.
CAP_LOG_ONLY = 0.6
CAP_PASSING_TEST = 0.9
INJECT_BELOW = 0.4


def record(
    connection: Connection,
    *,
    project_id: str,
    learning_kind: str,
    title: str,
    statement: str,
    detail: Mapping[str, Any],
    about: Mapping[str, Sequence[str]],
    evidence: Sequence[Mapping[str, Any]],
    confidence: float,
) -> dict[str, Any]:
    """Record one learned item; return it as the view payload (``KgLearningItem``).

    The ``record_learning`` tool's arguments, validated against ``DETAIL_FIELDS``. ``about`` is
    ``{symbols[], rules[], areas[]}`` and becomes ``about`` edges; ``evidence`` is at least one
    ``{type: log|test|diff|metric|file, ref, excerpt}``.

    - Provenance (agent role and name, run, workflow run, code task run, step, model) comes from
      ``llm_telemetry.get_llm_context()``, never from the arguments; ``kg_nodes.created_by`` is
      the agent role key, and a ``produced`` edge links the run.
    - The node is ``inferred``, quarantined and forgettable, like ``kg_propose_fact``.
    - Dedupe: ``dedupe_key`` = hash(kind, normalised error signature or statement, sorted
      ``about`` keys). A repeat increments ``seen_count``, appends evidence, sets
      ``last_seen_run`` and adds a ``produced`` edge from the new run — no second node.
    - Contradiction: a learning negating an open one on the same ``about`` closes the old one
      (``supersedes``).
    - Confidence is capped by evidence (``CAP_*``); no evidence refuses.
    - ``scrub.py`` runs on every body; a secret shape refuses the write.
    - Emits ``knowledge.learning_recorded`` ``{node_id, learning_kind, run_id, step}``.
    """
    raise NotImplementedError("learning_writeback.record: stream S2b (BOSSAPP-768)")


def write_back(
    connection: Connection,
    *,
    project_id: str,
    hook: str,
    observed: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Record what a pipeline hook observed; return the items written (possibly none).

    One call per hook (``HOOKS``), each a single line at its call site:

    - ``build_stage`` / ``repair_checkpoint``: a ``test_failure`` (failing test + error signature
      from the gate output) when a repair round starts; a ``fix`` linked ``fixed_by`` when the
      next gate passes, ``about`` the files and symbols the repair changed.
    - ``engine_turn``: a ``workaround`` when a build/deploy command failed and a later command in
      the same stage succeeded with a changed approach.
    - ``step_complete``: the step's typed outputs — ``investigation`` from investigate steps,
      ``review_finding`` from review steps, ``decision`` from typed decisions,
      ``root_cause``/``fix``/``precedent`` from ``incident_report`` — and the step's ``learned[]``.
    - ``agent_decide``: a ``decision`` with the chosen action's reasons and the reads it relied on.

    Every item goes through ``record()``. A hook never raises into the pipeline: a refused item
    (no evidence, a secret) is skipped with its reason logged.
    """
    raise NotImplementedError("learning_writeback.write_back: stream S2b (BOSSAPP-768)")
