"""Every selector, action and journey the browser walk relies on is declared and still in the console.

The incidents:
  * the first walk could not get past the org picker — its cards had no stable selector — so all
    thirty-six areas timed out behind it and the walk reported nothing useful;
  * the walk then chose actions by a CSS heuristic (class exactly `primary`), pressed eight
    buttons across thirty-six areas, never "Advance now" or "Create agent", and passed presses
    that did nothing.
So the walk's contract with the console is a table (console_contract.PRIMARY_ACTIONS, and
journeys.JOURNEY_SELECTORS). These tests enumerate every area the code declares and every testid
the walk uses, and fail with the file and the purpose when one goes missing.

REWRITTEN in the second round: the old "the walk never presses a destructive label" test guarded
the heuristic, which is gone; its intent survives below as a check on the declared testids.
"""
from __future__ import annotations

from e2e.harness import REPO_ROOT
from e2e.harness.console import DESTRUCTIVE, SOURCE_CONTRACT
from e2e.harness.console_contract import (PRIMARY_ACTIONS, Journey, NothingToPress, Press,
                                          fixed_start, rendered_in_source,
                                          declared_in_source, selector_testids)
from e2e.harness.discover import console_areas
from e2e.harness.journeys import JOURNEY_SELECTORS, JOURNEYS

WEB = REPO_ROOT / "apps" / "web"
# Read-only areas are allowed, never by default: each needs a sentence, and the number is held
# down so "nothing to press" cannot become the easy way past this test.
MAX_NOTHING_TO_PRESS = 8


def _source(rel: str) -> str:
    return (WEB / rel).read_text()


def test_every_selector_the_walk_uses_is_still_in_the_console():
    missing = []
    for rel, literal, purpose in SOURCE_CONTRACT:
        if literal not in _source(rel):
            missing.append(f"apps/web/{rel} no longer contains {literal!r} — the walk uses it for "
                           f"{purpose}")
    assert not missing, "\n".join(missing)


def test_every_console_area_declares_what_the_walk_presses_there():
    areas = [a.id for a in console_areas()]
    undeclared = [a for a in areas if a not in PRIMARY_ACTIONS]
    stale = [a for a in PRIMARY_ACTIONS if a not in areas]
    assert not undeclared, (f"areas with no primary action in console_contract.PRIMARY_ACTIONS: "
                            f"{undeclared} — declare a Press (testid + visible outcome), a "
                            f"Journey, or NothingToPress with the reason")
    assert not stale, f"PRIMARY_ACTIONS names areas ConsoleShell no longer has: {stale}"
    for area, entries in PRIMARY_ACTIONS.items():
        assert entries, f"{area}: an empty declaration"
        for entry in entries:
            if isinstance(entry, NothingToPress):
                assert len(entry.reason.split()) >= 6, f"{area}: say why there is nothing"


def test_read_only_areas_stay_the_exception():
    quiet = [a for a, entries in PRIMARY_ACTIONS.items()
             if all(isinstance(e, NothingToPress) for e in entries)]
    assert len(quiet) <= MAX_NOTHING_TO_PRESS, quiet


def test_every_declared_action_and_its_outcome_exist_in_the_console_source():
    missing = []
    for area, entries in PRIMARY_ACTIONS.items():
        for entry in entries:
            if not isinstance(entry, Press):
                continue
            if entry.paid_only and not (WEB / entry.source).exists():
                continue   # the Community export removes paid files; the walk skips these there
            if not declared_in_source(_source(entry.source), entry.testid):
                missing.append(f"{area}: [{entry.testid}] is not in apps/web/{entry.source}")
            for testid, _ in entry.fill:
                if not declared_in_source(_source(entry.source), testid):
                    missing.append(f"{area}: field [{testid}] is not in apps/web/{entry.source}")
            then_file = entry.then_source or entry.source
            for testid in entry.then:
                if not declared_in_source(_source(then_file), testid):
                    missing.append(f"{area}: follow-up [{testid}] is not in apps/web/{then_file}")
            outcome_file = entry.expect_source or entry.source
            for testid in selector_testids(entry.expect) + selector_testids(entry.grows):
                if not declared_in_source(_source(outcome_file), testid):
                    missing.append(f"{area}: outcome [{testid}] is not in apps/web/{outcome_file}")
            unless_file = entry.unless_source or outcome_file
            for testid in selector_testids(entry.unless):
                if not declared_in_source(_source(unless_file), testid):
                    missing.append(f"{area}: 'already done' [{testid}] is not in apps/web/{unless_file}")
            assert entry.expect or entry.grows or not entry.opens, \
                f"{area}: opening a <details> needs an expected content selector"
    assert not missing, "\n".join(missing)


def test_every_journey_is_reachable_from_an_area_and_its_selectors_exist():
    named = {e.name for entries in PRIMARY_ACTIONS.values() for e in entries
             if isinstance(e, Journey)}
    journeys = {name for _, name, _ in JOURNEYS}
    assert named == journeys, (f"journeys not tied to an area: {sorted(journeys - named)}; "
                               f"areas naming unknown journeys: {sorted(named - journeys)}")
    assert {"R1", "R2", "R3", "R4"} <= {req for req, _, _ in JOURNEYS}
    missing = []
    for journey, rel, testid in JOURNEY_SELECTORS:
        assert journey in journeys, journey
        text = _source(rel)
        literal = (f"data-testid={{`{testid}`}}" if "${" in testid
                   else f'data-testid="{testid}"')
        if literal not in text and not declared_in_source(text, testid):
            missing.append(f"{journey}: [{testid}] is not in apps/web/{rel}")
    assert not missing, "\n".join(missing)


def test_a_control_the_console_still_lacks_is_moved_to_the_contract_the_day_it_appears():
    """PENDING_SELECTORS are controls R2 needs that no screen has yet (an MCP server given to an
    agent). The journeys look for them and fail with the sentence; this fails when one shows up
    in its file, so it is promoted to JOURNEY_SELECTORS and held to existing from then on."""
    from e2e.harness.journeys import PENDING_SELECTORS

    journeys = {name for _, name, _ in JOURNEYS}
    arrived = []
    for journey, rel, testid, what in PENDING_SELECTORS:
        assert journey in journeys, journey
        assert len(what.split()) >= 5, f"{testid}: say what the console is missing"
        if rendered_in_source(_source(rel), testid):
            arrived.append(f"{testid} is now in apps/web/{rel}: move it from PENDING_SELECTORS "
                           f"to JOURNEY_SELECTORS")
    assert not arrived, "\n".join(arrived)


def test_a_pending_control_arrives_whatever_the_page_names_its_variable():
    """Round-three review: PENDING_SELECTORS said `aw-mcp-${server.id}` and the agents stream's
    wizard renders `aw-mcp-${server.mcp_server_id}`; the arrival check compared the whole
    literal, so the day the checkbox landed it would have stayed 'pending' and the journey's
    selector would never have been checked. Arrival is by the fixed start, exactly."""
    wizard = ('<input type="checkbox" data-testid={`aw-mcp-${server.mcp_server_id}`} />')
    assert rendered_in_source(wizard, "aw-mcp-${server.id}")
    assert rendered_in_source('<input data-testid={"aw-mcp-" + server.id} />', "aw-mcp-${x}")
    assert not rendered_in_source('<i data-testid={`aw-${kind}`} />', "aw-mcp-${server.id}")
    detail = ('{open ? <select data-testid="agent-mcp-select"/> : '
              '<button data-testid="agent-mcp-open">Give</button>}')
    assert rendered_in_source(detail, "agent-mcp-open")
    assert not rendered_in_source(detail, "agent-mcp-confirm")


def _module_functions_reached(fn, seen: set[str]) -> str:
    """The source of `fn` and of every journeys.py function it calls, however deep: the controls
    a journey presses through a helper (`_save_and_reopen` presses dw-save) are its own."""
    import inspect
    import types

    from e2e.harness import journeys as module

    if fn.__name__ in seen:
        return ""
    seen.add(fn.__name__)
    text = inspect.getsource(fn)
    codes = [fn.__code__]
    while codes:
        code = codes.pop()
        codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        for name in code.co_names:
            other = getattr(module, name, None)
            if isinstance(other, types.FunctionType) and other.__module__ == module.__name__:
                text += _module_functions_reached(other, seen)
    return text


def _pressed_in(code: str, testid: str) -> bool:
    if "${" in testid:
        return fixed_start(testid) + "{" in code    # f"aw-mcp-{ctx.mcp_server_id}"
    return f'"{testid}"' in code or f"'{testid}'" in code


def test_a_journey_labelled_with_a_requirement_uses_the_controls_that_are_that_requirement():
    """Round-two review: a palette smoke check was labelled R3 and an 'Advance now' press R4, so
    the report would call R3 and R4 proven whether or not a skill, an MCP tool or an agent was
    ever put on a step. Each requirement names the controls that ARE it (journeys.REQUIRES), and
    the journeys carrying its label must use every one."""
    from e2e.harness.journeys import PENDING_SELECTORS, REQUIRES

    used: dict[str, set[str]] = {}
    for journey, _, testid in JOURNEY_SELECTORS:
        used.setdefault(journey, set()).add(testid)
    for journey, _, testid, _ in PENDING_SELECTORS:
        used.setdefault(journey, set()).add(testid)
    gaps = []
    for requirement, controls in REQUIRES.items():
        labelled = [name for req, name, _ in JOURNEYS if req == requirement]
        assert labelled, f"no journey proves {requirement}"
        reached = set().union(*(used.get(name, set()) for name in labelled))
        # Round-three review: the table alone could be filled in for a smoke journey. The
        # journey's code — with every helper it calls — must also press the control.
        code = "".join(_module_functions_reached(fn, set())
                       for req, _, fn in JOURNEYS if req == requirement)
        for control in controls:
            if control not in reached:
                gaps.append(f"{requirement}: no journey labelled {requirement} "
                            f"({', '.join(labelled)}) declares [{control}]")
            if not _pressed_in(code, control):
                gaps.append(f"{requirement}: no journey labelled {requirement} "
                            f"({', '.join(labelled)}) presses [{control}] in its code")
    assert not gaps, "\n".join(gaps)


def test_a_smoke_journey_with_the_right_table_rows_is_still_not_proof():
    """The enforcement above reads code, not the table: a journey that only presses palette
    buttons does not press the controls R3 is made of, whatever rows name it."""
    from e2e.harness.journeys import REQUIRES, workflow_build

    code = _module_functions_reached(workflow_build, set())
    assert not _pressed_in(code, "dw-define-binding")
    assert not _pressed_in(code, "wf-pal-custom:${c.step_key}")
    assert _pressed_in(code, "wf-save")            # reached through _save_and_reopen
    assert all(_pressed_in(_module_functions_reached(fn, set()), c)
               for req, _, fn in JOURNEYS if req == "R3" for c in REQUIRES["R3"])


def test_the_walk_never_presses_a_control_that_destroys_or_stops_something():
    for label in ("Delete workflow", "Remove server", "Stop run", "Retire host", "Cancel",
                  "Disconnect YouTube", "Reject", "Sign out", "Disable skill", "Tear down"):
        assert DESTRUCTIVE.search(label), label
    for area, entries in PRIMARY_ACTIONS.items():
        for entry in entries:
            if isinstance(entry, Press):
                words = entry.testid.replace("-", " ").replace("*", "")
                assert not DESTRUCTIVE.search(words), f"{area}: {entry.testid} looks destructive"


class _Control:
    def __init__(self, page, selector: str):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def wait_for(self, **_):
        return None

    def fill(self, text, **_):
        self.page.did.append(("fill", self.selector, text))

    def click(self, **_):
        self.page.did.append(("click", self.selector))

    def get_attribute(self, _name):
        return None

    def count(self):
        return 1 if self.selector.startswith('[data-testid="done"]') else 0

    def is_visible(self):
        return False


class _TwiceRenderedPage:
    """A screen that renders every control twice — a hidden dock copy and the visible one."""

    def __init__(self):
        self.did: list[tuple] = []

    def locator(self, selector: str):
        return _Control(self, selector)

    def evaluate(self, _script):
        return ""

    def wait_for_timeout(self, _ms):
        return None


def test_a_press_types_into_and_presses_only_the_visible_instance_of_each_control():
    """Round-two review: the target and the follow-ups were pressed on the VISIBLE match, but
    the fields were filled on `.first` — the hidden dock copy on a twice-rendered screen — so
    the visible form was submitted empty. Every control a press touches is a visible match."""
    from e2e.harness.console import ScreenResult, Walk

    page = _TwiceRenderedPage()
    entry = Press("agent-send", "app/x.tsx", expect='[data-testid="done"]',
                  fill=(("agent-input", "hello"),), then=("agent-confirm",))
    result = ScreenResult("action", "x", "u")
    Walk.__new__(Walk)._press(page, entry, result)
    assert not result.problems, result.problems
    touched = [d for d in page.did if d[0] in ("fill", "click")]
    assert [d[0] for d in touched] == ["fill", "click", "click"]
    for action in touched:
        assert action[1].endswith(">> visible=true"), action
