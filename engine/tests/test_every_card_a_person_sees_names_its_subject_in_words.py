"""Every decision a person sees names its subject in words, shows no raw identifier, and gives a
recommended answer or the reason only a person can answer.

The operator's rule (2026-11-07): only real decisions reach a person, always in plain words, with
context. Three cards on the laptop broke it at once — "Restart the step, or cancel the build."
(which build? why?), "Reconcile 4 expired budget reservations from knowledge.embed (knowledge)"
(a skill key, and housekeeping no person should be asked) and a squad "stuck" on work that was
moving again. Each was fixed at its producer; this test keeps the class closed.

It walks every place in engine/ and apps/ that raises a decision (the same producer scan as
test_every_decision_boss_raises_has_its_buttons_decided) and reads the QUESTION each writes:

  * there is one, and it is words — not a bare reason code;
  * no literal part carries a raw identifier (`crun_…`, `act_…`, `knowledge.embed`);
  * no interpolated value is an identifier (`{run['id']}`, `{changeset_id}`, `{skill_id}`,
    `{kind}`) — a card names the work by its title, never by its key.

The runtime half — `decision_words.card_problems`, which also requires a recommended answer or
a reason it needs a person — is applied to the cards the Shepherd, the budget governor and the
cap refusal raise, on Postgres, in test_cards_name_the_work_and_withdraw_themselves.py.
"""
from __future__ import annotations

import ast
from typing import Optional

from boss_engine.services import decision_words as words

from . import test_every_decision_boss_raises_has_its_buttons_decided as scan

#: Interpolated names that hold a key, not words.
_ID_NAMES = {"id", "kind", "skill", "skill_id", "effect_kind", "tool_name", "reg"}


#: Helpers that turn a key or a title into words: what they return is fit for a card.
_WORDS = {"key_words", "_key_words", "humanize_code", "spend_subject", "short_title",
          "_short_title", "_title"}


def _is_identifier(expr: ast.expr) -> Optional[str]:
    """The identifier an f-string value interpolates, or None when it is words."""
    if isinstance(expr, ast.Call) and scan._called_name(expr) in _WORDS:
        return None
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr in ("strip", "lower", "upper", "title"):
        return _is_identifier(expr.func.value)
    if isinstance(expr, ast.Name):
        n = expr.id
    elif isinstance(expr, ast.Attribute):
        n = expr.attr
    elif isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) \
            and isinstance(expr.slice.value, str):
        n = expr.slice.value
    else:
        return None
    if n in _ID_NAMES or n.endswith("_id") or n.endswith("_ids") or n.endswith("_key"):
        return ast.unparse(expr)
    return None


def _question_expr(call: ast.Call) -> Optional[ast.expr]:
    if scan._called_name(call) == "open_step_decision":
        return next((kw.value for kw in call.keywords if kw.arg == "question"),
                    call.args[2] if len(call.args) > 2 else None)
    return call.args[3] if len(call.args) > 3 else next(
        (kw.value for kw in call.keywords if kw.arg == "question"), None)


def _problems(module: scan.Module, expr: Optional[ast.expr], func, depth: int = 0,
              part: bool = False) -> list[str]:
    """`part`: a piece of a question joined with others (it may be empty on its own)."""
    if expr is None:
        return ["no question"]
    if depth > 3:
        return []
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [p for p in words.card_problems({"question": expr.value or ("x y" if part else ""),
                                                "recommendation": {"recommended": "-"}})
                if not (part and p == "no subject in words")]
    if isinstance(expr, ast.JoinedStr):
        out = []
        literal = "".join(v.value for v in expr.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
        out += [p for p in words.card_problems({"question": literal or "x y",
                                                "recommendation": {"recommended": "-"}})
                if p.startswith("raw identifier")]
        for v in expr.values:
            if isinstance(v, ast.FormattedValue):
                ident = _is_identifier(v.value)
                if ident:
                    out.append(f"interpolates the identifier {{{ident}}}")
        return out
    if isinstance(expr, ast.IfExp):
        return (_problems(module, expr.body, func, depth, part) +
                _problems(module, expr.orelse, func, depth, part))
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return (_problems(module, expr.left, func, depth, True) +
                _problems(module, expr.right, func, depth, True))
    if isinstance(expr, ast.Name) and func is not None:
        assigned = scan._assignments(func, expr.id, before=expr.lineno)
        return [p for value in assigned for p in _problems(module, value, func, depth + 1)]
    # A helper's result (card_question(...), clean_text(...)): words are its own job, pinned
    # by its own tests and by card_problems at run time.
    return []


def _findings() -> list[str]:
    out = []
    for module in scan.MODULES:
        for node in ast.walk(module.tree):
            if not (isinstance(node, ast.Call) and scan._called_name(node) in scan.PRODUCER_CALLS):
                continue
            func = module.enclosing_function(node)
            if func is not None and (module.rel, func.name) in scan.SINKS:
                continue
            for p in _problems(module, _question_expr(node), func):
                out.append(f"{module.rel}:{node.lineno}: {p}")
    return sorted(set(out))


def test_the_scan_reads_every_producer_question():
    seen = 0
    for module in scan.MODULES:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and scan._called_name(node) in scan.PRODUCER_CALLS:
                seen += 1
    assert seen > 80, "the producer scan no longer finds the places that raise decisions"


def test_every_question_names_its_subject_in_words_without_raw_identifiers():
    problems = _findings()
    assert not problems, (
        "These places raise a decision a person reads with a raw identifier or no words. Name "
        "the work by its title (decision_words.spend_subject, a short title), never its key:\n  "
        + "\n  ".join(problems))


def test_card_problems_catches_the_three_laptop_cards():
    assert words.card_problems({"question": "Restart the step, or cancel the build.",
                                "recommendation": {"action": "stuck_work_needs_person"}}) \
        == ["no recommended answer and no reason it needs a person"]
    got = words.card_problems({"question": "Reconcile 4 expired budget reservations from "
                                           "knowledge.embed (knowledge): a call started",
                               "recommendation": {"action": "x"}})
    assert "raw identifier 'knowledge.embed'" in got
    assert words.card_problems({"question": "Build crun_de5396b5a6b04224a158 is stuck",
                                "recommendation": {"recommended": "Retry it"}}) \
        == ["raw identifier 'crun_de5396b5a6b04224a158'"]
    assert words.card_problems({"question": "boss.deploy.yaml dropped 11 rules on Order API",
                                "recommendation": {"recommended": "Keep them"}}) == []
    assert words.card_problems({"question": "Open draft PR bumping esbuild…: it keeps starting "
                                            "over. Recommended: open its log, then retry it.",
                                "recommendation": {"recommended": "Retry it",
                                                   "why_a_person": "BOSS tried 3 fixes"}}) == []
