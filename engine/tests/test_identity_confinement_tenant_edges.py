"""The tenant edges of central confinement (security review of PR #305).

  * an org-shared kind is really the organisation's: a project kind slipped into
    object_rules.ORG_SHARED_KINDS would weaken the guard and its generated test together;
  * a provider sign-in belongs to the org it was started for: another org's start is refused,
    never handed the live session, and the loader places it at its org;
  * an id several projects hold (a voice transcript key, unique per org only) is refused, never
    passed as "names nothing";
  * repo_id and host_id name every kind their handlers read.
"""
from __future__ import annotations

import time

import pytest

from boss_engine.identity import object_kinds, object_rules, scope as scope_module
from boss_engine.identity.object_rules import Confine, Confined, _confine_one
from boss_engine.services import subscription_sign_in as sign_in

#: Org-shared kinds whose rows DO carry a project, each with why the org may still use them.
SHARED_WITH_A_PROJECT_COLUMN = {
    "deployment_host": "a verified host is usable by every project of its org "
                       "(deployment_fleet.usable_hosts_sql); project_id records who added it",
}


def test_every_org_shared_kind_is_owned_by_the_org():
    assert set(SHARED_WITH_A_PROJECT_COLUMN) <= object_rules.ORG_SHARED_KINDS
    wrong = []
    for kind in sorted(object_rules.ORG_SHARED_KINDS):
        spec = object_kinds.get(kind)
        org_owned = ((isinstance(spec, object_kinds.OrgKind) and spec.project == "NULL")
                     or isinstance(spec, object_kinds.InProcessOrgKind))
        if not org_owned and kind not in SHARED_WITH_A_PROJECT_COLUMN:
            wrong.append(kind)
    assert not wrong, ("an org-shared kind must be an OrgKind with no project (or an "
                       f"InProcessOrgKind), or be justified above: {wrong}")


# ---- a provider sign-in is its org's -----------------------------------------------------------

def _live(monkeypatch, org_id: str) -> sign_in._Session:
    now = time.time()
    session = sign_in._Session(sign_in_id="si_edge_live", provider="codex", project_id="p_one",
                               actor="someone", started=now, expires=now + 600, state="waiting",
                               verification_url="https://auth.openai.com/x", user_code="ABCD",
                               org_id=org_id)
    monkeypatch.setitem(sign_in._SESSIONS, session.sign_in_id, session)
    return session


def test_another_orgs_start_is_refused_not_handed_the_live_sign_in(monkeypatch):
    live = _live(monkeypatch, "org_one")
    with pytest.raises(sign_in.SignInError) as refused:
        sign_in.start("codex", project_id="p_two", org_id="org_two", actor="other",
                      signed_in=lambda: False, argv=("true",))
    assert refused.value.status == 409
    assert "ABCD" not in str(refused.value.detail)
    assert "another organization" not in str(refused.value.detail.get("headline") or "").lower()
    assert "another organization" not in str(refused.value.detail.get("fix") or "").lower()
    assert live.state == "waiting"                     # theirs is not killed either
    with pytest.raises(sign_in.SignInError):
        sign_in.start("codex", project_id="p_two", org_id="org_two", actor="other", fresh=True,
                      signed_in=lambda: False, argv=("true",))
    assert live.state == "waiting"


def test_the_same_orgs_start_is_shown_the_live_sign_in(monkeypatch):
    _live(monkeypatch, "org_one")
    out = sign_in.start("codex", project_id="p_three", org_id="org_one", actor="colleague",
                        signed_in=lambda: False, argv=("true",))
    assert out["sign_in_id"] == "si_edge_live"


def test_a_sign_in_is_placed_at_its_org(monkeypatch):
    _live(monkeypatch, "org_one")
    spec = object_kinds.get("subscription_sign_in")
    assert spec.load(None, "si_edge_live") == ("org_one", None)
    assert spec.load(None, "si_nothing") is None


# ---- an id several projects hold is refused ----------------------------------------------------

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


RULE = Confine("body", "transcript_id", ("task_voice_transcript",))


def test_a_transcript_key_held_in_two_projects_in_same_org_is_refused(monkeypatch):
    db = _Rows([("org_one", "p_one"), ("org_one", "p_two")])
    spec = object_kinds.get("task_voice_transcript")
    assert spec.load(db, "t1", org_id="org_one") == object_kinds.AMBIGUOUS
    with pytest.raises(Confined):
        _confine_one(RULE, "t1", org_id="org_one", project_id="p_one", db=db)
    # Scope.of(..., ...) -> scope.resolve must also raise ScopeNotFound on AMBIGUOUS.
    monkeypatch.setitem(scope_module._OBJECT_LOADERS, "task_voice_transcript", spec.load)  # noqa: SLF001
    with pytest.raises(scope_module.ScopeNotFound):
        scope_module.resolve(
            scope_module.Scope.of("task_voice_transcript", body="transcript_id"),
            scope_module.RequestValues(path_params={}, query={}, body={"transcript_id": "t1"}),
            org_id="org_one", db=db)


def test_a_transcript_key_held_in_two_different_orgs_resolves_for_caller_org():
    db = _Rows([("org_one", "p_one"), ("org_two", "p_two")])
    spec = object_kinds.get("task_voice_transcript")
    assert spec.load(db, "t1", org_id="org_one") == ("org_one", "p_one")
    _confine_one(RULE, "t1", org_id="org_one", project_id="p_one", db=db)
    with pytest.raises(Confined):
        _confine_one(RULE, "t1", org_id="org_one", project_id="p_other", db=db)


def test_a_transcript_key_held_once_is_placed_at_its_project():
    db = _Rows([("org_one", "p_one")])
    _confine_one(RULE, "t1", org_id="org_one", project_id="p_one", db=db)
    with pytest.raises(Confined):
        _confine_one(RULE, "t1", org_id="org_one", project_id="p_other", db=db)


# ---- names reach every kind their handlers read ------------------------------------------------

def test_repo_id_and_host_id_name_every_kind_their_handlers_read():
    assert "github_repository_binding" in object_kinds.kinds_for_id("repo_id")
    assert set(object_kinds.kinds_for_id("host_id")) == {"deployment_host", "agent_host"}
