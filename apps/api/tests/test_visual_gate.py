"""APP-4 (#94) — deterministic visual/UX gate: the rubric checks, the evaluate verdict, and the
wiring that turns the `required_not_run` no-op into a real gate that blocks the release go/no-go."""
from apps.api.app import main as api_main
from apps.api.app.schemas import FileChange
from apps.api.app.services import visual_gate as vg

_GOOD = ("<html><body><header><nav>Home</nav></header><h1>Stronger bodies for defence aspirants</h1>"
         "<section><h2>Plans</h2><p>Coaching from Rs 1,499/mo for Tier-2 cities.</p></section>"
         "<button>Get started</button></body></html>")
_BLANK = "<html><body><div id='root'></div></body></html>"
_ERROR = "<html><body><h1>Application error: a client-side exception has occurred</h1></body></html>"
_PLACEHOLDER = "<html><body><h1>Your headline here</h1><section><p>Lorem ipsum dolor sit amet</p></section></body></html>"


# --- rubric primitives -------------------------------------------------------
def test_visible_text_and_real_content():
    assert "Stronger bodies" in vg.visible_text(_GOOD)
    assert vg.has_real_content(_GOOD) and not vg.has_real_content(_BLANK)


def test_error_page_detection():
    assert vg.is_error_page(_ERROR)
    assert vg.is_error_page(_GOOD, console_errors=["TypeError: x"]) is True
    assert not vg.is_error_page(_GOOD)


def test_placeholder_and_hierarchy():
    assert vg.has_placeholder_copy(_PLACEHOLDER) and not vg.has_placeholder_copy(_GOOD)
    assert vg.has_visual_hierarchy(_GOOD) and not vg.has_visual_hierarchy(_BLANK)


def test_mobile_overflow():
    assert vg.mobile_overflows({"mobile": {"scrollWidth": 500, "clientWidth": 375}}) is True
    assert vg.mobile_overflows({"mobile": {"scrollWidth": 375, "clientWidth": 375}}) is False
    assert vg.mobile_overflows(None) is None            # not measured (served-HTML evidence)


# --- evaluate ----------------------------------------------------------------
def test_evaluate_none_is_not_applicable():
    assert vg.evaluate_visual(None)["status"] == "not_applicable"


def test_evaluate_passes_a_coherent_page():
    v = vg.evaluate_visual({"html": _GOOD, "route": "/"})
    assert v["status"] == "passed" and v["findings"] == []


def test_evaluate_fails_error_blank_and_placeholder():
    assert vg.evaluate_visual({"html": _ERROR})["status"] == "failed"
    assert vg.evaluate_visual({"html": _BLANK})["status"] == "failed"
    ph = vg.evaluate_visual({"html": _PLACEHOLDER})
    assert ph["status"] == "failed" and any("placeholder" in f for f in ph["findings"])


def test_evaluate_fails_on_mobile_overflow():
    v = vg.evaluate_visual({"html": _GOOD, "viewport": {"mobile": {"scrollWidth": 600, "clientWidth": 375}}})
    assert v["status"] == "failed" and any("overflow" in f for f in v["findings"])
    assert v["checks"]["no_mobile_overflow"] is False


def test_hierarchy_is_soft_when_not_required():
    html = "<html><body><p>Real enough content here to pass the length threshold for sure.</p></body></html>"
    assert vg.evaluate_visual({"html": html}, require_hierarchy=True)["status"] == "failed"
    assert vg.evaluate_visual({"html": html}, require_hierarchy=False)["status"] == "passed"


def test_evidence_refs_from_screenshot():
    v = vg.evaluate_visual({"html": _GOOD, "screenshot_ref": "art_123"})
    assert v["evidence_refs"] == ["art_123"]


# --- wiring: real gate + release blocking ------------------------------------
def test_browser_validation_review_becomes_real_with_evidence():
    sf = api_main.software_factory
    files = [FileChange(path="apps/web/app/page.tsx", content="x")]
    # no evidence -> still the honest required_not_run
    assert sf._build_browser_validation_review(files)["status"] == "required_not_run"
    # with evidence -> a real verdict
    good = sf._build_browser_validation_review(files, render_evidence={"html": _GOOD})
    assert good["status"] == "passed"
    bad = sf._build_browser_validation_review(files, render_evidence={"html": _ERROR})
    assert bad["status"] == "failed" and bad["findings"]
    # non-UI change -> not applicable
    assert sf._build_browser_validation_review([FileChange(path="api/x.py", content="y")])["status"] == "not_applicable"


def test_failed_visual_gate_blocks_release():
    sf = api_main.software_factory
    common = dict(validation_status="passed", validation_summary="ok",
                  security_dependency_review={"status": "passed"}, evidence_refs=[])
    failed = sf._build_release_go_no_go(browser_validation={"status": "failed"}, **common)
    assert failed["status"] == "no_go" and "visual_gate_failed" in failed["blockers"]
    # required_not_run is still only a warning (unchanged)
    warn = sf._build_release_go_no_go(browser_validation={"status": "required_not_run"}, **common)
    assert warn["status"] == "conditional_go"
    passed = sf._build_release_go_no_go(browser_validation={"status": "passed"}, **common)
    assert passed["status"] == "go"


# --- endpoint ----------------------------------------------------------------
def test_endpoint(client):
    resp = client.post("/v1/visual-gate", json={"evidence": {"html": _ERROR}})
    assert resp.status_code == 200 and resp.json()["status"] == "failed"
    assert client.post("/v1/visual-gate", json={}).json()["status"] == "not_applicable"
