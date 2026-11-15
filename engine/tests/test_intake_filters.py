"""The route filter language (P9a-2, LLD §5.2): every leaf op, the booleans, the limits, the
`matches` engine (RE2 or the capped `regex` fallback, never Python `re`), `changed`, derived
fields, and the per-trigger-version cache with its TTL and invalidation."""
from __future__ import annotations

import time

import pytest

from boss_engine.services import intake_filters as f

ENVELOPE = {
    "specversion": "1.0", "id": "ev1", "source": "slack://T1", "type": "com.slack.message.app_mention",
    "subject": "C0SUPPORT/1700000000.1", "time": "2026-11-14T10:00:00Z",
    "data": {"channel": "C0SUPPORT", "text": "Hello @BOSS, the payments job failed", "n": 7,
             "labels": ["bug", "urgent"], "changed_fields": ["priority", "state"],
             "nested": {"deep": {"value": "x"}}},
    "bossactor": {"kind": "user", "external_id": "U1", "email": "priya@ACME.com"},
    "bossscreen": {"verdict": "clean"},
}


def m(spec, env=ENVELOPE):
    return f.compile_filter(spec).matches(env)


def leaf(field, op, value=None):
    out = {"field": field, "op": op}
    if value is not None:
        out["value"] = value
    return out


# ---- leaves -------------------------------------------------------------------------------------

def test_eq_ne_compare_strings_numbers_and_booleans_by_type():
    assert m(leaf("type", "eq", "com.slack.message.app_mention"))
    assert not m(leaf("type", "eq", "com.slack.message.im"))
    assert m(leaf("data.n", "eq", 7)) and m(leaf("data.n", "eq", 7.0))
    assert m(leaf("data.n", "ne", 8))
    assert m(leaf("bossscreen.verdict", "ne", "suspicious"))
    assert not m(leaf("data.missing", "eq", "x")) and m(leaf("data.missing", "ne", "x"))


def test_in_and_nin_take_lists_and_match_list_fields_elementwise():
    assert m(leaf("type", "in", ["com.slack.message.app_mention", "com.slack.message.im"]))
    assert m(leaf("data.labels", "in", ["urgent"]))
    assert m(leaf("data.labels", "nin", ["wontfix"]))
    assert not m(leaf("data.channel", "in", ["C0OTHER"]))
    with pytest.raises(f.FilterError):
        f.compile_filter(leaf("type", "in", "not-a-list"))


def test_text_ops_are_case_insensitive():
    assert m(leaf("data.text", "contains", "PAYMENTS JOB"))
    assert m(leaf("data.text", "prefix", "hello"))
    assert m(leaf("data.channel", "suffix", "support"))
    assert not m(leaf("data.text", "contains", "refund"))


def test_exists_and_comparisons_over_numbers_and_instants():
    assert m(leaf("data.channel", "exists"))
    assert m(leaf("data.missing", "exists", False))
    assert m(leaf("data.n", "gt", 5)) and m(leaf("data.n", "lte", 7)) and not m(leaf("data.n", "lt", 7))
    assert m(leaf("time", "gte", "2026-11-14T09:00:00Z"))
    assert m(leaf("time", "lt", "2026-11-15T00:00:00+00:00"))
    assert not m(leaf("data.text", "gt", 3))          # text against a number: no order, False
    with pytest.raises(f.FilterError):
        f.compile_filter(leaf("data.n", "gt", "not a number or instant"))


def test_changed_reads_the_record_sources_changed_fields():
    assert m(leaf("changed", "changed", "priority"))
    assert m(leaf("data.changed_fields", "changed", ["assignee", "state"]))
    assert not m(leaf("changed", "changed", "assignee"))
    with pytest.raises(f.FilterError):
        f.compile_filter(leaf("changed", "changed", 7))


def test_derived_email_domain_and_deep_paths():
    assert m(leaf("bossactor.email_domain", "eq", "acme.com"))
    assert m(leaf("data.nested.deep.value", "eq", "x"))
    assert m(leaf("data.labels.0", "eq", "bug"))


# ---- booleans and limits --------------------------------------------------------------------------

def test_all_any_not_nest_and_the_shape_is_checked():
    spec = {"all": [leaf("type", "prefix", "com.slack."),
                    {"any": [leaf("data.channel", "eq", "C0SUPPORT"), leaf("data.channel", "eq", "C1")]},
                    {"not": leaf("data.text", "matches", "(?i)^\\s*ignore")}]}
    compiled = f.compile_filter(spec)
    assert compiled.leaves == 4 and compiled.matches(ENVELOPE)
    assert not compiled.matches({**ENVELOPE, "data": {**ENVELOPE["data"], "text": "  IGNORE this"}})
    assert m({"all": []}) and not m({"any": []})
    for bad in ({"all": "x"}, {"all": [leaf("a", "eq", 1)], "field": "a"},
                {"field": "a"}, {"field": "a", "op": "like", "value": 1},
                {"field": " a", "op": "eq", "value": 1}, "text", 7):
        with pytest.raises(f.FilterError):
            f.compile_filter(bad)


def test_depth_and_leaf_limits():
    deep = leaf("a", "eq", 1)
    for _ in range(f.MAX_DEPTH):
        deep = {"not": deep}
    with pytest.raises(f.FilterError, match="deeper"):
        f.compile_filter(deep)
    wide = {"all": [leaf("a", "eq", i) for i in range(f.MAX_LEAVES + 1)]}
    with pytest.raises(f.FilterError, match="more than"):
        f.compile_filter(wide)
    assert f.compile_filter({"all": [leaf("a", "eq", i) for i in range(f.MAX_LEAVES)]}).leaves == 64


def test_empty_filter_matches_everything():
    assert m(None) and m({}) and f.compile_filter({}).leaves == 0


# ---- matches: RE2 or the capped fallback ------------------------------------------------------------

def test_matches_uses_re2_or_the_regex_fallback_never_re():
    assert f.REGEX_ENGINE in ("re2", "regex"), "the API needs google-re2 or regex installed"
    assert m(leaf("data.text", "matches", "(?i)payments\\s+job"))
    assert not m(leaf("data.text", "matches", "^refund"))


def test_a_pattern_over_2kb_is_refused_and_a_bad_pattern_names_the_problem():
    with pytest.raises(f.FilterError, match="2048"):
        f.compile_filter(leaf("data.text", "matches", "a" * 2049))
    with pytest.raises(f.FilterError, match="matches"):
        f.compile_filter(leaf("data.text", "matches", "(unclosed"))
    with pytest.raises(f.FilterError):
        f.compile_filter(leaf("data.text", "matches", ""))


@pytest.mark.skipif(f.REGEX_ENGINE != "regex", reason="RE2 has no catastrophic backtracking to cap")
def test_a_catastrophic_pattern_is_cut_off_at_the_2ms_budget_and_answers_false():
    compiled = f.compile_filter(leaf("data.text", "matches", "(a+)+$"))
    env = {"data": {"text": "a" * 5000 + "b"}}
    started = time.perf_counter()
    assert compiled.matches(env) is False
    assert time.perf_counter() - started < 0.2
    assert compiled.timeouts == 1


def test_explain_names_which_leaf_held():
    compiled = f.compile_filter({"all": [leaf("type", "prefix", "com.slack."),
                                         leaf("data.channel", "eq", "C1")]})
    got = compiled.explain(ENVELOPE)
    assert got["matched"] is False
    assert [x["held"] for x in got["leaves"]] == [True, False]
    assert got["leaves"][1]["actual"] == "C0SUPPORT"


# ---- the cache ------------------------------------------------------------------------------------

def test_compiled_filters_are_cached_per_version_and_invalidated(monkeypatch):
    f.invalidate()
    spec = leaf("type", "eq", "x")
    a = f.compiled_for("atrg_1", "v1", spec)
    assert f.compiled_for("atrg_1", "v1", spec) is a            # same version: cached
    b = f.compiled_for("atrg_1", "v2", leaf("type", "eq", "y"))  # new version: recompiled
    assert b is not a and b.matches({"type": "y"})
    f.invalidate("atrg_1")
    assert f.compiled_for("atrg_1", "v2", spec) is not b          # dropped
    assert f.cache_size() == 1
    monkeypatch.setattr(f, "CACHE_TTL_SECONDS", 0.0)
    c = f.compiled_for("atrg_1", "v2", spec)
    assert f.compiled_for("atrg_1", "v2", spec) is not c          # ttl passed: recompiled
    f.invalidate()
    assert f.cache_size() == 0
