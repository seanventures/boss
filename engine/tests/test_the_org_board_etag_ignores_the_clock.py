"""The org board's ETag moves on what happened, never on the clock.

An agent with an act in progress has `busy_seconds` = now − started_at, which ticks every second;
when it was in the ETag, two reads of an unchanged board a second apart got different ETags (the
304 test failed whenever its reads straddled a second).
"""
from boss_engine.services.agents_org_board import etag_of


def _board(busy: int, open_acts: list[str], done: int = 0) -> dict:
    return {
        "project_id": "p", "days": 7, "generated_at": f"2026-10-15T09:00:{busy % 60:02d}Z",
        "agents": [{"role_key": "forge", "busy_seconds": busy,
                    "work": {"done": done, "failed": 0, "open": len(open_acts)},
                    "open": [{"act_id": a, "since": "2026-10-15T07:00:00Z"} for a in open_acts]},
                   {"role_key": "scout", "busy_seconds": 5, "work": {"done": 1, "failed": 0,
                                                                     "open": 0}, "open": []}],
        "teams": [{"key": "eng", "members": ["forge", "scout"], "work": {"open": len(open_acts)},
                   "members_load": sorted([{"role_key": "forge", "busy_seconds": busy},
                                           {"role_key": "scout", "busy_seconds": 5}],
                                          key=lambda x: (-x["busy_seconds"], x["role_key"]))}],
    }


def test_an_open_act_ticking_does_not_change_the_etag():
    assert etag_of(_board(7242, ["a1"])) == etag_of(_board(7243, ["a1"]))


def test_load_order_flipping_as_time_passes_does_not_change_the_etag():
    # forge passes scout's 5 s: members_load reorders, nothing happened
    assert etag_of(_board(4, ["a1"])) == etag_of(_board(6, ["a1"]))


def test_an_act_ending_changes_the_etag():
    assert etag_of(_board(7242, ["a1"])) != etag_of(_board(7242, [], done=1))


def test_a_new_act_changes_the_etag():
    assert etag_of(_board(7242, ["a1"])) != etag_of(_board(7242, ["a1", "a2"]))


def test_the_board_itself_still_shows_busy_seconds():
    board = _board(7242, ["a1"])
    etag_of(board)
    assert board["agents"][0]["busy_seconds"] == 7242
    assert board["teams"][0]["members_load"][0] == {"role_key": "forge", "busy_seconds": 7242}
