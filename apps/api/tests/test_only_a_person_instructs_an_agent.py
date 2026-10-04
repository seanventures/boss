"""Only a person may write an agent's instructions; an agent cannot (spec A8, E6)."""
from __future__ import annotations

import pytest

from apps.api.app import main as api_main
from apps.api.tests.instructions_support import MARKER, PROJECT, add, make_agent


def test_an_agent_actor_is_refused_in_the_engine(client, engine):
    from boss_engine.services import agent_instructions

    key = make_agent(client)
    with engine.begin() as c:
        with pytest.raises(agent_instructions.InstructionError) as refused:
            agent_instructions.add(c, PROJECT, key, actor="agent:agt_0123456789", name="x",
                                   body=MARKER)
    assert refused.value.status == 403
    assert refused.value.message == "Only a person can instruct an agent"


def test_keys_without_the_right_permission_are_refused(client, engine, monkeypatch):
    reader, reviewer, granter = "ins_reader", "ins_reviewer", "ins_granter"
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ins-r,ins-v,ins-g,ins-ceo")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        f"ins-r:{reader},ins-v:{reviewer},ins-g:{granter},ins-ceo:ceo")
    with engine.begin() as c:
        for member, perms in ((reader, ["agent_chat"]), (reviewer, ["role_review"]),
                              (granter, ["role_review", "workflow_manage"])):
            api_main.members.upsert_member(c, project_id=PROJECT, member_id=member,
                                           display_name=member, member_type="human",
                                           role_keys=[], permissions=perms, actor="ceo")
    auth = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
    made = client.post("/v1/agents", json={"project_id": PROJECT, "name": "Keyed Agent",
                                           "role_key": "ins_keyed_agent"}, headers=auth("ins-ceo"))
    assert made.status_code == 201, made.text
    key = "ins_keyed_agent"
    assert add(client, key, MARKER, headers=auth("ins-r")).status_code == 403
    assert add(client, key, MARKER, headers=auth("ins-v")).status_code == 201
    # The Passport asks, without a denial on record, whether to show the controls.
    listing = lambda k: client.get(f"/v1/agents/{key}/instructions",  # noqa: E731
                                   params={"project_id": PROJECT}, headers=auth(k)).json()
    assert listing("ins-r")["can_edit"] is False and listing("ins-v")["can_edit"] is True
    assert len(listing("ins-r")["items"]) == 1
    skill_md = "---\nname: release-notes\ndescription: Writes release notes\n---\nUse headings."
    body = {"project_id": PROJECT, "filename": "SKILL.md", "text": skill_md, "grant_to": key}
    assert client.post("/v1/skills/from-file", json=body,
                       headers=auth("ins-v")).status_code == 403
    assert client.post("/v1/skills/from-file", json=body,
                       headers=auth("ins-g")).status_code == 201
