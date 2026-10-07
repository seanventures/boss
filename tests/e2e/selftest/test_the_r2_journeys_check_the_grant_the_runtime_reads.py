"""R2's MCP half is proven by the grant the runtime reads, through the controls the page really has.

The incident (round-three review): on create, a ticked `aw-mcp-<id>` checkbox was taken as proof
the agent held the server — nothing read the agent→MCP grant back. On update, the journey looked
for `agent-mcp-select` without pressing `agent-mcp-open`, the button the agents stream renders in
its place, so it would have said "no way to give an MCP server" when there was one; after
confirming it neither waited nor read `agent-mcp-failed`; and after the reload it checked the
held list only when it was rendered — an agent holding nothing shows `agent-mcp-empty` instead —
so a failed grant passed silently.

The page below is a model of the agent page's MCP row as it is now — the Passport's Loadout
(app/skills/agent-abilities.tsx, agent-loadout spec §3.2): the server picker exists only after the
row's `+ Add` (`loadout-mcp-add`) is pressed, picking a server IS the grant, a refusal is a toast,
and an agent holding nothing shows `No MCP` (`loadout-no-mcp`) instead of chips. The incident's
three lessons carry over unchanged: press the button that opens the picker, wait for the row or
the refusal, and check the row after a reload whether or not it has chips. No browser, no API.
"""
from __future__ import annotations

import re
from typing import Optional

import pytest

from e2e.harness import journeys


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now


TOAST_ERROR = "__toast_error__"


class _McpRow:
    def __init__(self, servers: dict[str, str], *, refuse: str = "", grant_lands: bool = True):
        self.servers = servers                  # id -> name
        self.holds: list[str] = []
        self.open = False
        self.failed = ""
        self.refuse, self.grant_lands = refuse, grant_lands
        self.grants: set[str] = set()           # what the API (role-mcp-servers) holds
        self.pressed: list[str] = []

    def rendered(self) -> dict[str, str]:
        shown: dict[str, str] = {"loadout-mcp-add": "Add ▾"}
        chips = [f"{self.servers[h]} · all tools" for h in self.holds]
        shown["loadout-mcp"] = "MCP " + (" ".join(chips) if chips else "No MCP") + " Add ▾"
        for h in self.holds:
            shown[f"agent-mcp-{h}"] = f"{self.servers[h]} · all tools"
        if not self.holds:
            shown["loadout-no-mcp"] = "No MCP"
        if self.open:
            for sid, name in self.servers.items():
                if sid not in self.holds:
                    shown[f"loadout-mcp-pick-{sid}"] = name
        if self.failed:
            shown[TOAST_ERROR] = self.failed
        return shown

    def press(self, testid: str) -> None:
        self.pressed.append(testid)
        if testid == "loadout-mcp-add":
            self.open = True
        elif testid.startswith("loadout-mcp-pick-"):
            self.open = False
            if self.refuse:
                self.failed = self.refuse
                return
            pick = testid[len("loadout-mcp-pick-"):]
            if self.grant_lands:
                self.grants.add(pick)
            self.holds.append(pick)            # the worst case: the row shows what it sent


class _Locator:
    def __init__(self, page: "_Page", selector: str):
        self.page, self.selector = page, selector
        found = re.search(r'data-testid(\^?)="([^"]+)"', selector)
        self.prefix = bool(found and found.group(1))
        self.testid = TOAST_ERROR if "toast-error" in selector else (found.group(2) if found else "")

    def _matches(self) -> list[str]:
        shown = self.page.row.rendered()
        if self.prefix:
            return [t for t in shown if t.startswith(self.testid)]
        return [self.testid] if self.testid in shown else []

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    def count(self):
        return len(self._matches())

    def wait_for(self, state="visible", timeout=0):
        if not self._matches():
            raise TimeoutError(f"{self.selector} never became {state}")

    def click(self, **_):
        self.wait_for()
        self.page.row.press(self.testid)

    def inner_text(self, timeout=0):
        self.wait_for()
        return self.page.row.rendered()[self._matches()[0]]

    def all_inner_texts(self):
        return [self.page.row.rendered()[t] for t in self._matches()]


class _Page:
    def __init__(self, row: _McpRow, clock: _Clock):
        self.row, self.clock = row, clock

    def locator(self, selector: str):
        return _Locator(self, selector)

    def wait_for_timeout(self, ms):
        self.clock.now += ms / 1000


@pytest.fixture
def world(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(journeys, "time", clock)
    section = _McpRow({"srv-1": "e2e-mcp-101500", "srv-2": "e2e-mcp-101500-update"})
    ctx = journeys.Context(stamp="101500", fakes_url=None, target=object(),
                           mcp_server_id="srv-1", mcp_name="e2e-mcp-101500")
    grants: dict[str, Optional[set[str]]] = {"api": section.grants}
    monkeypatch.setattr(journeys, "_mcp_grants", lambda ctx, role: grants["api"])
    monkeypatch.setattr(journeys, "_second_mcp_server",
                        lambda ctx: ("srv-2", "e2e-mcp-101500-update"))
    return section, _Page(section, clock), ctx, grants


def test_the_picker_behind_its_add_button_is_found_and_the_server_given(world):
    """The Loadout renders `loadout-mcp-pick-<id>` only after the row's `+ Add` is pressed;
    round three looked for the picker first and would have reported the control missing."""
    section, page, ctx, _ = world
    problems: list[str] = []
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems)
    assert problems == []
    assert given == ("srv-1", "e2e-mcp-101500")
    assert section.pressed == ["loadout-mcp-add", "loadout-mcp-pick-srv-1"]


def test_a_server_the_agent_already_holds_from_create_is_not_offered_again(world):
    """When create worked the agent holds the R1 server and the picker offers nothing more, so
    the update gives a second server it does not hold."""
    section, page, ctx, _ = world
    section.holds = ["srv-1"]
    section.grants.add("srv-1")
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert problems == [] and given == ("srv-2", "e2e-mcp-101500-update")


def test_a_refused_grant_is_reported_with_the_pages_own_sentence(world):
    section, page, ctx, _ = world
    section.refuse = "You may not give this agent that server (403)."
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert given == ("", "")
    assert problems and "may not give this agent that server" in problems[0]


def test_a_grant_that_never_reached_the_runtime_fails_even_when_the_page_shows_it(world):
    """The page lists what it sent; the run gets what role-mcp-servers holds."""
    section, page, ctx, _ = world
    section.grant_lands = False
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert problems == [] and given[0] == "srv-1"
    assert journeys._mcp_not_listed_after_reload(page, "e2e_agent", *given) == ""
    said = journeys._mcp_not_held(ctx, "e2e_agent", *given, "was given it on its page")
    assert "does not hold MCP server e2e-mcp-101500" in said


def test_an_agent_holding_nothing_after_the_reload_fails_the_check(world):
    """Round three checked the held list only when it was rendered; an agent holding nothing
    shows `No MCP` instead, so a lost grant passed silently."""
    section, page, ctx, _ = world
    said = journeys._mcp_not_listed_after_reload(page, "e2e_agent", "srv-1", "e2e-mcp-101500")
    assert "after a reload" in said and "No MCP" in said


def test_a_server_whose_name_starts_another_held_one_is_not_taken_for_it(world):
    """`e2e-mcp-101500` is the start of `e2e-mcp-101500-update`: holding the second is not
    holding the first, though the row's text contains both names."""
    section, page, ctx, _ = world
    section.holds = ["srv-2"]
    said = journeys._mcp_not_listed_after_reload(page, "e2e_agent", "srv-1", "e2e-mcp-101500")
    assert "after a reload" in said


def test_an_install_without_the_grant_api_says_so_rather_than_passing(world):
    _, _, ctx, grants = world
    grants["api"] = None
    said = journeys._mcp_not_held(ctx, "e2e_agent", "srv-1", "e2e-mcp-101500", "was created")
    assert said == journeys.NO_MCP_GRANT_API


def test_create_reads_the_grant_back_instead_of_trusting_the_tick():
    """The create journey's success path names the read-back, so a tick alone can never be the
    evidence again."""
    import inspect

    code = inspect.getsource(journeys.agent_with_skill)
    assert "_mcp_not_held(" in code
    tick = code.index("mcp_box.first.check()")
    assert code.index("_mcp_not_held(", tick) > tick
