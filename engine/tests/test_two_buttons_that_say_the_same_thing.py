"""A button is what its label says.

Reported from the dock, verbatim on screen:

    [ Fix consent link so drafts can launch ]
      Extends run crun_6eec724c252b4a828da4 on its existing branch and PR #194
    [ Fix consent link so drafts can launch ]
      Extends the existing app on branch boss/code/crun_e161609e077445e4986c and PR #194
    [ Redeploy to staging ]

Two runs of the same app, both landing on PR #194 — the same outcome offered twice under one
label. `_same_offer` compared tool and ARGUMENTS, and the `run_id`s differ, so both survived.

The operator's read is the right one: "we are not handling the duplicates in the chat whenever
we are providing the actions." A person choosing between two identical labels is being asked to
guess, and the arguments they cannot see are not an answer.
"""

from boss_engine.services.intent_router import remember_offer


def _labels(offers):
    out = []
    for o in offers:
        remember_offer(out, o)
    return [x["label"] for x in out]


class TestTwoButtonsThatSayTheSameThing:
    def test_the_reported_case(self):
        """Same label, same tool, different run — one button."""
        assert _labels([
            {"tool": "extend_code_task", "label": "Fix consent link so drafts can launch",
             "args": {"run_id": "crun_6eec724c252b4a828da4"}},
            {"tool": "extend_code_task", "label": "Fix consent link so drafts can launch",
             "args": {"run_id": "crun_e161609e077445e4986c"}},
            {"tool": "redeploy", "label": "Redeploy to staging",
             "args": {"target_id": "deptgt_af3d508f8fa74dac9b22"}},
        ]) == ["Fix consent link so drafts can launch", "Redeploy to staging"]

    def test_the_first_one_wins(self):
        """A continuation's phrasing is not better for having come second, and the button
        somebody has started reading must not rename itself underneath them."""
        out = []
        remember_offer(out, {"tool": "redeploy", "label": "Redeploy to staging",
                             "args": {"target_id": "a"}})
        remember_offer(out, {"tool": "redeploy", "label": "redeploy to staging",
                             "args": {"target_id": "b"}})
        assert len(out) == 1
        assert out[0]["args"]["target_id"] == "a"

    def test_whitespace_and_case_do_not_make_it_a_different_button(self):
        assert len(_labels([
            {"tool": "redeploy", "label": "Redeploy  to staging", "args": {"t": 1}},
            {"tool": "redeploy", "label": "redeploy to staging", "args": {"t": 2}},
        ])) == 1

    def test_the_same_args_under_two_labels_is_still_one_button(self):
        """The original failure this dedupe was written for — "Approve Change Calendar (PR #184)"
        beside "Approve merge gate — Change Calendar PR #184 (base)". Both rules are needed."""
        assert len(_labels([
            {"tool": "decide", "label": "Approve Change Calendar (PR #184)", "args": {"id": "d1"}},
            {"tool": "decide", "label": "Approve merge gate — Change Calendar PR #184",
             "args": {"id": "d1"}},
        ])) == 1

    def test_different_tools_are_never_merged(self):
        """Extending a build and redeploying it are different acts even if a model labels them
        alike; collapsing those would hide a real option rather than a duplicate."""
        assert len(_labels([
            {"tool": "extend_code_task", "label": "Fix it", "args": {"run_id": "a"}},
            {"tool": "redeploy", "label": "Fix it", "args": {"target_id": "b"}},
        ])) == 2

    def test_genuinely_distinct_offers_all_survive(self):
        """The failure mode of a dedupe is eating a real choice."""
        assert len(_labels([
            {"tool": "decide", "label": "Approve Change Calendar", "args": {"id": "d1"}},
            {"tool": "decide", "label": "Approve Lead-Triage Inbox", "args": {"id": "d2"}},
            {"tool": "decide", "label": "Approve Interview Scorecard", "args": {"id": "d3"}},
        ])) == 3

    def test_an_unlabelled_offer_falls_back_to_comparing_arguments(self):
        """A missing label must not make every unlabelled offer identical to every other."""
        assert len(_labels([
            {"tool": "redeploy", "label": "", "args": {"target_id": "a"}},
            {"tool": "redeploy", "label": "", "args": {"target_id": "b"}},
        ])) == 2
