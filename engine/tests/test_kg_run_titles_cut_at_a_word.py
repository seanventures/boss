"""A run's title is cut at a word, and a stored one the old builder cut mid-word is mended on read
(KG tester round 6: the Rules review scan filter read "…(retry with verif…")."""
from boss_engine.services.knowledge.titles import RUN_TITLE_MAX, repair_cut, word_cut
from boss_engine.services.knowledge.view import node_payload

LONG = ("Business rules scan: loanaccount transaction processors (retry with verified evidence "
        "after the first scan found none)")


def test_a_title_that_fits_is_whole():
    assert word_cut("B3 · Incident: repayments post more") == "B3 · Incident: repayments post more"


def test_a_long_title_is_cut_at_a_word_within_the_limit():
    cut = word_cut(LONG)
    assert len(cut) < RUN_TITLE_MAX
    assert cut.endswith("…")
    head = cut[:-1]
    assert LONG.startswith(head) and LONG[len(head)] == " ", cut


def test_a_single_long_word_is_cut_by_character():
    cut = word_cut("x" * 200)
    assert cut.endswith("…") and len(cut) < RUN_TITLE_MAX


def test_a_name_the_old_builder_cut_mid_word_is_mended_on_read():
    old = LONG[:89].rstrip() + "…"
    assert len(old) == RUN_TITLE_MAX and old[-2].isalnum()
    mended = repair_cut(f"{old} · Oct 24, 19:50")
    title, when = mended.rsplit(" · ", 1)
    assert when == "Oct 24, 19:50"
    assert LONG.startswith(title[:-1]) and LONG[len(title) - 1] == " "


def test_a_word_cut_title_is_left_as_it_is():
    name = f"{word_cut(LONG)} · Oct 24, 19:50"
    assert repair_cut(name) == name
    assert repair_cut("Conversation · Oct 24, 19:50") == "Conversation · Oct 24, 19:50"


def test_a_run_node_is_served_mended():
    old = LONG[:89].rstrip() + "…"
    row = {"id": "kgn_1", "node_key": "run:wfr_1", "kind": "run", "name": f"{old} · Oct 24, 19:50", "attrs": {}}
    label = node_payload(row)["label"]
    assert label != row["name"] and label.endswith(" · Oct 24, 19:50")
