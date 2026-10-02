"""R2's MCP half is proven by the grant the runtime reads, through the controls the page really has.

The incident (round-three review): on create, a ticked `aw-mcp-<id>` checkbox was taken as proof
the agent held the server — nothing read the agent→MCP grant back. On update, the journey looked
for `agent-mcp-select` without pressing `agent-mcp-open`, the button the agents stream renders in
its place, so it would have said "no way to give an MCP server" when there was one; after
confirming it neither waited nor read `agent-mcp-failed`; and after the reload it checked the
held list only when it was rendered — an agent holding nothing shows `agent-mcp-empty` instead —
so a failed grant passed silently.

The page below is a model of the agents stream's MCP section (agent-detail-ui.tsx): the picker
exists only after "Give this agent an MCP server" is pressed, and the held list is replaced by a
sentence when the agent holds nothing. No browser, no API.
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


class _McpSection:
    def __init__(self, servers: dict[str, str], *, refuse: str = "", grant_lands: bool = True):
        self.servers = servers                  # id -> name
        self.holds: list[str] = []
        self.open = False
        self.pick = ""
        self.failed = ""
        self.refuse, self.grant_lands = refuse, grant_lands
        self.grants: set[str] = set()           # what the API (role-mcp-servers) holds
        self.pressed: list[str] = []

    def rendered(self) -> dict[str, str]:
        shown: dict[str, str] = {}
        if self.holds:
            shown["agent-mcp-held"] = " ".join(self.servers[h] for h in self.holds)
        else:
            shown["agent-mcp-empty"] = "It holds no MCP server."
        if self.failed:
            shown["agent-mcp-failed"] = self.failed
        if self.open:
            shown["agent-mcp-select"] = ""
            shown["agent-mcp-confirm"] = "Give it this server"
        elif any(s not in self.holds for s in self.servers):
            shown["agent-mcp-open"] = "Give this agent an MCP server"
        return shown

    def press(self, testid: str) -> None:
        self.pressed.append(testid)
        if testid == "agent-mcp-open":
            self.open = True
        elif testid == "agent-mcp-confirm":
            if self.refuse:
                self.failed = self.refuse
                return
            self.open = False
            if self.grant_lands:
                self.grants.add(self.pick)
            self.holds.append(self.pick)       # the page shows what it sent, either way


class _Locator:
    def __init__(self, page: "_Page", selector: str):
        self.page, self.selector = page, selector
        found = re.search(r'data-testid="([^"]+)"', selector)
        self.testid = found.group(1) if found else ""

    @property
    def first(self):
        return self

    def _here(self) -> bool:
        return self.testid in self.page.section.rendered()

    def count(self):
        return 1 if self._here() else 0

    def wait_for(self, state="visible", timeout=0):
        if not self._here():
            raise TimeoutError(f"{self.selector} never became {state}")

    def click(self, **_):
        self.wait_for()
        self.page.section.press(self.testid)

    def select_option(self, value, timeout=0):
        if value not in self.page.section.servers or value in self.page.section.holds:
            raise TimeoutError(f"no option {value}")
        self.page.section.pick = value

    def inner_text(self, timeout=0):
        self.wait_for()
        return self.page.section.rendered()[self.testid]

    def locator(self, _sub):
        return self

    def evaluate_all(self, _script):
        return [{"value": k, "text": v, "group": ""} for k, v in self.page.section.servers.items()
                if k not in self.page.section.holds]


class _Page:
    def __init__(self, section: _McpSection, clock: _Clock):
        self.section, self.clock = section, clock

    def locator(self, selector: str):
        return _Locator(self, selector)

    def wait_for_timeout(self, ms):
        self.clock.now += ms / 1000


@pytest.fixture
def world(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(journeys, "time", clock)
    section = _McpSection({"srv-1": "e2e-mcp-101500", "srv-2": "e2e-mcp-101500-update"})
    ctx = journeys.Context(stamp="101500", fakes_url=None, target=object(),
                           mcp_server_id="srv-1", mcp_name="e2e-mcp-101500")
    grants: dict[str, Optional[set[str]]] = {"api": section.grants}
    monkeypatch.setattr(journeys, "_mcp_grants", lambda ctx, role: grants["api"])
    monkeypatch.setattr(journeys, "_second_mcp_server",
                        lambda ctx: ("srv-2", "e2e-mcp-101500-update"))
    return section, _Page(section, clock), ctx, grants


def test_the_picker_behind_its_open_button_is_found_and_the_server_given(world):
    """The agents stream renders `agent-mcp-select` only after `agent-mcp-open` is pressed;
    round three looked for the select first and would have reported the control missing."""
    section, page, ctx, _ = world
    problems: list[str] = []
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems)
    assert problems == []
    assert given == ("srv-1", "e2e-mcp-101500")
    assert section.pressed == ["agent-mcp-open", "agent-mcp-confirm"]


def test_a_server_the_agent_already_holds_from_create_is_not_offered_again(world):
    """When create worked the agent holds the R1 server and the page offers nothing more, so the
    update gives a second server it does not hold."""
    section, page, ctx, _ = world
    section.holds = ["srv-1"]
    section.grants.add("srv-1")
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert problems == [] and given == ("srv-2", "e2e-mcp-101500-update")


def test_a_refused_grant_is_reported_with_the_pages_own_sentence(world):
    section, page, ctx, _ = world
    section.refuse = "The API refused: you may not give this agent that server (403)."
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert given == ("", "")
    assert problems and "may not give this agent that server" in problems[0]


def test_a_grant_that_never_reached_the_runtime_fails_even_when_the_page_shows_it(world):
    """The page lists what it sent; the run gets what role-mcp-servers holds."""
    section, page, ctx, _ = world
    section.grant_lands = False
    given = journeys._give_mcp_on_page(page, ctx, "e2e_agent", problems := [])
    assert problems == [] and given[0] == "srv-1"
    assert journeys._mcp_not_listed_after_reload(page, "e2e_agent", given[1]) == ""
    said = journeys._mcp_not_held(ctx, "e2e_agent", *given, "was given it on its page")
    assert "does not hold MCP server e2e-mcp-101500" in said


def test_an_agent_holding_nothing_after_the_reload_fails_the_check(world):
    """Round three checked the held list only when it was rendered; an agent holding nothing
    shows `agent-mcp-empty` instead, so a lost grant passed silently."""
    section, page, ctx, _ = world
    said = journeys._mcp_not_listed_after_reload(page, "e2e_agent", "e2e-mcp-101500")
    assert "after a reload" in said and "holds no MCP server" in said


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
