"""Instructions are bounded, and over the bound they are refused, never trimmed (spec §4.5, E4)."""
from __future__ import annotations

from apps.api.tests.instructions_support import add, make_agent, stored


def test_one_file_over_16k_is_too_long(client, engine):
    key = make_agent(client)
    refused = add(client, key, "Check the tests. " * 1000)
    assert refused.status_code == 422
    assert refused.json()["detail"]["message"] == "Too long · 16k max"
    assert stored(engine, key) == 0


def test_the_budget_is_per_mode_and_refused_when_exceeded(client, engine):
    key = make_agent(client)
    chunk = "Check every migration has a rollback.\n" * 300            # ≈ 11.4k characters
    assert add(client, key, chunk, name="a.md", modes=["work"]).status_code == 201
    over = add(client, key, chunk, name="b.md")
    assert over.status_code == 422 and over.json()["detail"]["message"] == "Over the 4k budget"
    # The same text for chat only does not crowd work.
    assert add(client, key, chunk, name="c.md", modes=["chat"]).status_code == 201
    assert stored(engine, key) == 2


def test_at_most_eight_files_and_no_binary_and_unique_names(client, engine):
    key = make_agent(client)
    for i in range(8):
        assert add(client, key, f"Rule {i}: be brief.", name=f"r{i}.md").status_code == 201
    ninth = add(client, key, "Rule 9: be kind.", name="r9.md")
    assert ninth.status_code == 422 and ninth.json()["detail"]["message"] == "At most 8 files"
    other = make_agent(client)
    binary = add(client, other, "abc\x00def")
    assert binary.status_code == 422 and binary.json()["detail"]["message"] == "Not a text file"
    assert add(client, other, "Be brief.", name="Rules").status_code == 201
    taken = add(client, other, "Be kind.", name="rules")
    assert taken.status_code == 409 and taken.json()["detail"]["message"] == "Name taken"


def test_a_dry_run_stores_nothing_and_says_the_cost(client, engine):
    key = make_agent(client)
    dry = add(client, key, "Check every PR for tests.", dry_run=True)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["ok"] is True and body["tokens_est"] == 7 and body["looks_like_skill"] is False
    assert body["budget_after"]["work"]["used_chars"] == len("Check every PR for tests.")
    assert stored(engine, key) == 0
    skill_md = "---\nname: release-notes\ndescription: Writes release notes\n---\nUse headings."
    assert add(client, key, skill_md, dry_run=True).json()["looks_like_skill"] is True
