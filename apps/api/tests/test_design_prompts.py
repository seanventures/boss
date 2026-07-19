"""APP-2 (#92) — design/product role + design-aware prompts: UI detection, the product_design role
prompt, and the impl/review clauses that only fire for UI-bearing tasks (additive — back-end tasks
are byte-identical to before)."""
from apps.api.app.services import design_prompts as dp
from apps.api.app.services.governed_code_task_service import _impl_prompt, _review_prompt


# --- UI detection ------------------------------------------------------------
def test_is_ui_bearing_by_project_kind():
    assert dp.is_ui_bearing({"project_kind": "web"}, "Anything") is True
    assert dp.is_ui_bearing({"project_kind": "spa"}, "") is True


def test_is_ui_bearing_by_file_extension():
    assert dp.is_ui_bearing({}, "Add a widget", test_files=["src/App.tsx"]) is True
    assert dp.is_ui_bearing({"target_files": ["styles/home.css"]}, "") is True


def test_is_ui_bearing_by_keywords():
    assert dp.is_ui_bearing({"instructions": "Build a responsive landing page with Tailwind"}, "Site") is True
    assert dp.is_ui_bearing({}, "Build the marketing page hero section") is True


def test_not_ui_bearing_for_backend():
    assert dp.is_ui_bearing({"instructions": "Add a token-bucket rate limiter in Python"},
                            "Rate limiter", test_files=["test_rl.py"]) is False


# --- the product_design role prompt ------------------------------------------
def test_product_design_prompt_covers_the_axis():
    p = dp.product_design_prompt("Fitness site", "Coaching for defence aspirants", ["hero", "pricing"])
    low = p.lower()
    assert "type scale" in low and "hierarchy" in low and "responsive" in low
    assert "component" in low                       # compose the kit
    assert "lorem" in low and "placeholder" in low  # forbids filler
    assert "value proposition" in low               # on-domain copy


def test_product_design_prompt_threads_design_system():
    p = dp.product_design_prompt("t", "i", [], design_system="--accent: 221 83% 53%")
    assert "--accent" in p and "do not invent a parallel one" in p.lower()


# --- clauses -----------------------------------------------------------------
def test_impl_clause_makes_presentation_non_strippable():
    c = dp.design_impl_clause().lower()
    assert "behavior only" in c and "not strip presentation" in c
    assert "placeholder" in c or "lorem" in c


def test_review_clause_puts_incoherence_in_scope():
    c = dp.design_review_clause().lower()
    assert "incoherence is in scope" in c and "refuted" in c
    assert "placeholder" in c and "responsive" in c and "hierarchy" in c


# --- integration: additive to the pipeline prompts ---------------------------
def test_impl_prompt_is_byte_identical_when_not_ui():
    assert _impl_prompt("t", "i", ["a_test.py"], "pytest", ui=False) == \
           _impl_prompt("t", "i", ["a_test.py"], "pytest")


def test_impl_prompt_gains_design_axis_when_ui():
    p = _impl_prompt("t", "i", ["App.tsx"], "npm run build", ui=True)
    assert "DESIGN AXIS" in p and "BEHAVIOR only" in p
    assert dp.design_impl_clause() in p               # the full clause is appended


def test_review_prompt_additive_design_clause():
    base = _review_prompt("t", ["c"])
    assert _review_prompt("t", ["c"], ui=False) == base           # byte-identical off
    ui = _review_prompt("t", ["c"], ui=True)
    assert "DESIGN AXIS" in ui and "in scope" in ui.lower()
    assert "VERDICT: SURVIVED" in ui                              # still ends with the verdict contract
