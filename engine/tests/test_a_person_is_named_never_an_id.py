"""Who did something is said by name, never by id (services/people_names.py).

The Factory's build list said "Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs".
"""
from __future__ import annotations

import inspect
import uuid

from boss_engine.services import factory_view, people_names


class _Conn:
    def __init__(self, names):
        self.names = names

    def execute(self, _sql, params):
        return type("R", (), {"scalar": lambda _self: self.names.get(params["i"])})()


def _fresh_id(prefix="usr"):
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def test_an_id_becomes_the_persons_name():
    pid = _fresh_id()
    assert people_names.who(pid, _Conn({pid: "Paramaguru Gnanaguru"})) == "Paramaguru Gnanaguru"


def test_an_unknown_id_is_a_person_never_the_id():
    pid = _fresh_id()
    assert people_names.who(pid, _Conn({})) == "a person"
    assert people_names.who(_fresh_id("agt"), _Conn({})) == "an agent"


def test_the_viewer_is_you_and_words_pass_through():
    pid = _fresh_id()
    assert people_names.who(pid, _Conn({}), viewer=pid) == "you"
    assert people_names.who("ceo") == "the owner"
    assert people_names.who("BOSS") == "BOSS"


def test_stopped_by_names_the_person(monkeypatch):
    monkeypatch.setattr(people_names, "_lookup", lambda actor, connection=None: "Paramaguru")
    assert factory_view._who_stopped({"cancelled_by": _fresh_id()}, None) == "Stopped by Paramaguru"
    monkeypatch.setattr(people_names, "_lookup", lambda actor, connection=None: None)
    said = factory_view._who_stopped({"cancelled_by": "usr_kdzhawzy7mxmp5hpb3bqlgqojs"}, None)
    assert said == "Stopped by a person" and "usr_" not in said


def test_wiki_docs_holds_and_watches_name_the_person_and_keep_the_id():
    """The read shows the name. The stored id is still the audit record."""
    from boss_engine.services import documents, wiki, work_holds, work_watchdogs

    pid = _fresh_id()
    conn = _Conn({pid: "Ada Lovelace"})
    revision = wiki._revision({
        "id": "wkr_1", "revision_no": 1, "title": "Deploys", "body": "Daily.",
        "content_hash": "h", "author_kind": "person", "author_id": pid,
        "source_note": "", "consent_id": None, "sources": [], "restored_from": None,
        "created_at": None,
    }, conn)
    assert revision["author_id"] == pid and revision["author_name"] == "Ada Lovelace"
    comment = documents._comment({"id": "c1", "author": pid, "body": "Who owns it?",
                                  "created_at": None}, conn)
    assert comment["author"] == pid and comment["author_name"] == "Ada Lovelace"
    hold = work_holds._public({
        "id": "hold_1", "project_id": "p", "root_kind": "work_item", "root_id": "w",
        "mode": "pause", "status": "active", "reason": "spend", "created_by": pid,
        "created_at": None, "restored_by": None, "restored_at": None,
    }, conn)
    assert hold["created_by"] == pid and hold["created_by_name"] == "Ada Lovelace"
    dog = work_watchdogs.view({
        "id": "wdg_1", "project_id": "p", "root_item_id": "w", "watcher": pid,
        "conditions": ["blocked"], "stall_hours": 24, "instructions": "",
        "contact_point_ids": [], "enabled": True, "fired_count": 0,
        "last_fired_at": None, "last_fired": None, "created_at": None,
    }, conn)
    assert dog["watcher"] == pid and dog["watcher_name"] == "Ada Lovelace"
    # A word that is not a principal id is left for the console's own words.
    plain = wiki._revision({**revision, "author_id": "ceo", "author_name": None}, conn)
    assert plain["author_id"] == "ceo" and "author_name" not in plain


def test_stored_reasons_name_people_at_write_time():
    from boss_engine.services import code_task_service, governed_host_deploy, system_doctor
    for module in (code_task_service, governed_host_deploy, system_doctor):
        src = inspect.getsource(module)
        assert "people_names import who" in src, module.__name__
    assert 'f"Stopped by {actor}"' not in inspect.getsource(code_task_service)
