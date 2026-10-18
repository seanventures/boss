"""A talk answers; it does not act (D2, 2026-10-18).

On a failed step the operator asked the step's agent to retry the delivery. It replied "I'm
retrying now: pushing the saved branch … [act:github.create_pr_real]" — a talk has no tools,
nothing ran. Every talk turn now says it cannot act and where the action is done.
"""
from boss_engine.services import agent_converse as ac


def test_every_talk_reply_is_told_it_cannot_act():
    plan = ac._Plan(project_id="p", conversation_id="c", actor="a", mode="reply",
                    speakers=[], person_turn_id=None, rounds=1, reserve_micros=0)
    conv = {"kind": "talk", "seats": ["backend_engineer"]}
    said = ac._instruction(plan, conv, ac._Speaker(role_key="backend_engineer", how="asked_by_person"),
                           {}, False, [])
    assert ac.TALK_CANNOT_ACT in said
    assert "Never say or imply you are doing" in ac.TALK_CANNOT_ACT
    assert "Retry" in ac.TALK_CANNOT_ACT and "Talk to BOSS" in ac.TALK_CANNOT_ACT
