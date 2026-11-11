"""The mutation probe does not fight the first pass's test scope, and re-probes the same mutant.

Pre-mortem finding 5: pass 1's test author is told "no P2 (edge cases, error paths)", yet a
boundary-flip mutant failed `tests_strong_enough`, which bought up to two rounds that changed
nothing and shipped anyway on advisory profiles. Each re-probe planted a NEW bug, and a mutant
could land in a connector's pre-tested files or in config no criterion covers.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import mutation_probe as mp


def test_pass_one_strength_is_advisory_and_pass_two_owns_it():
    assert mp.strength_is_advisory(more_tests_pass=False) is True
    assert mp.strength_is_advisory(more_tests_pass=True) is False


def test_on_pass_one_a_surviving_mutant_is_never_a_failed_gate():
    src = inspect.getsource(g._run_governed_code_task)
    assert ('if mutation_caught is not None and not _strength_advisory:\n'
            '                gates["tests_strong_enough"] = mutation_caught') in src
    assert src.count('gates["tests_strong_enough"] = mutation_caught') == 1
    assert '"tests_strength": ({"advisory": True' in src
    # So no warm round is spent on it: the gate a round would be for is never failing.
    assert g.warm_fix_rounds([], configured=2, repair_attempts=0, use_microvm=False) == 0


def test_targets_skip_connector_files_config_and_files_no_criterion_covers():
    impl = ["app/src/routes/tickets.ts", "app/src/auth/google.ts", "app/vite.config.ts",
            "app/package.json", "app/README.md", "app/src/lib/format.ts", "app/prisma/seed.ts",
            "app/src/index.ts"]
    text = {"app/src/routes/tickets.ts": "export function assignTicket() {}",
            "app/src/lib/format.ts": "export const pad = (n) => n",
            "app/src/index.ts": "app.use(tickets)"}
    got = mp.targets(impl, ["An agent can assign a ticket to themselves"],
                     connector_files=["src/auth/google.ts", "lib/index.ts"], read=text.get)
    assert got == ["app/src/routes/tickets.ts", "app/src/index.ts"]
    # A generic name in the connector (index.ts) does not exclude the app's own by name alone,
    # only by its path.
    assert "app/src/index.ts" in mp.targets(["app/src/index.ts"], [],
                                            connector_files=["lib/index.ts"])


def test_with_no_covered_file_every_source_file_stays_a_target():
    assert mp.targets(["a/x.ts", "a/y.py", "a/z.css"], ["Zebra striping"]) == ["a/x.ts", "a/y.py"]


def test_the_same_mutant_is_planted_again():
    before = "export function inRange(n) {\n  return n >= 1 && n <= 5;\n}\n"
    mutated = before.replace("n >= 1", "n > 1")
    mutant = mp.capture({"src/r.ts": before}, {"src/r.ts": mutated})
    # The suite was strengthened (tests changed), the implementation was not: same bug again.
    assert mp.reapply(before, mutant["src/r.ts"]) == mutated
    # The implementer also changed the file elsewhere: the mutant still lands exactly.
    moved = "// header\n" + before
    assert mp.reapply(moved, mutant["src/r.ts"]) == "// header\n" + mutated
    # The line it mutated is gone: no guess, the caller asks for a new mutant.
    assert mp.reapply("export const x = 1;\n", mutant["src/r.ts"]) is None


def test_a_fix_round_re_probes_the_same_mutant():
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    reprobe = block[block.index('if "tests_strong_enough" in failing:\n                        # THE WEAK'):]
    assert "mutation_probe.reapply(_orig[p], b)" in reprobe
    assert "if _same_mutant:" in reprobe
    assert '"same_mutant": _same_mutant' in reprobe
    first = inspect.getsource(g._run_governed_code_task)
    assert "_mutant = mutation_probe.capture(" in first


def test_the_plant_is_deterministic_code_never_a_model_session():
    """BOSSAPP-1004 (audit 997 §7.7): the probe used to spend a WRITE-CAPABLE model session to
    flip one operator ($13.93/month), while the re-probe already replanted without a model.
    Both the first plant and a fix round's fresh plant are now mutation_probe.plant_first."""
    first = inspect.getsource(g._run_governed_code_task)
    probe = first[first.index("check tests (mutation)"):first.index("IaC VERIFY LANE")]
    assert "mutation_probe.plant_first(" in probe
    assert ".run_role(" not in probe, "the probe spends a model session again"
    assert '_route("check_tests")' not in probe
    # No plantable site: the probe is recorded as not run — never guessed, never a model.
    assert "mutation probe not run: no deterministic mutation site" in probe
    # The micro-VM substrate plants through a test command in the suite's own VM (echo engine).
    assert "mutation_probe.apply_command(_planted)" in probe
    assert 'engine_override="echo"' in probe
    src = inspect.getsource(g)
    block = src[src.index("_convergeable = _CONVERGEABLE"):src.index("rounds_used = max(0")]
    reprobe = block[block.index('if "tests_strong_enough" in failing:'):]
    assert "mutation_probe.plant_first(" in reprobe
    assert "_route(\"check_tests\")" not in reprobe, "the re-probe still routes a model"
    assert "_mutate_prompt" not in src, "the model mutator prompt is retired"
