"""APP-5 (#95) — whole-repo coherence via retrieval: design-file pinning, relevance ranking, and
the safety ceiling that replaces first-40-by-path. Pure/hermetic."""
from apps.api.app.services import repo_retriever as rr


# --- design-file detection ---------------------------------------------------
def test_is_design_file():
    for f in ("tailwind.config.ts", "src/index.css", "components.json",
              "src/components/ui/button.tsx", "app/globals.css", "src/lib/utils.ts"):
        assert rr.is_design_file(f), f
    for f in ("src/api/handlers.py", "server/routes.ts", "README.md"):
        assert not rr.is_design_file(f), f


# --- relevance ---------------------------------------------------------------
def test_score_relevance_weights_path_over_content():
    tt = rr.terms("fix the checkout pricing calculation")
    path_hit = rr.score_relevance("src/checkout/pricing.ts", "unrelated body", tt)
    content_hit = rr.score_relevance("src/misc.ts", "pricing checkout logic here", tt)
    assert path_hit > content_hit > 0
    assert rr.score_relevance("a.ts", "b", set()) == 0.0     # no task terms -> no signal


# --- selection ---------------------------------------------------------------
def _entries(*specs):
    return [{"rel": r, "content": c, "size": len(c)} for r, c in specs]


def test_ui_task_always_pins_design_files_past_the_cap():
    entries = _entries(("tailwind.config.ts", "theme"), ("src/index.css", "tokens"),
                       ("src/components/ui/button.tsx", "Button"))
    entries += _entries(*[(f"src/page{i}.tsx", "x") for i in range(200)])
    sel = rr.select_files(entries, "add a pricing page", ui=True, max_files=10)
    picked = {s["rel"] for s in sel["selected"]}
    for design in ("tailwind.config.ts", "src/index.css", "src/components/ui/button.tsx"):
        assert design in picked                              # pinned regardless of the 10-file cap
    assert sel["pinned_count"] == 3


def test_relevance_orders_non_pinned():
    entries = _entries(("src/auth/login.ts", "login form"),
                       ("src/checkout/pricing.ts", "compute totals"),
                       ("src/random/thing.ts", "nothing"))
    sel = rr.select_files(entries, "fix checkout pricing", ui=False)
    order = [s["rel"] for s in sel["selected"]]
    assert order[0] == "src/checkout/pricing.ts"             # most relevant first, not by path


def test_byte_and_file_ceiling_on_non_pinned():
    big = "x" * 50_000
    entries = _entries(*[(f"f{i}.ts", big) for i in range(20)])
    sel = rr.select_files(entries, "unrelated", ui=False, max_files=5, max_bytes=100_000, head_bytes=10_000)
    assert len(sel["selected"]) <= 5 and sel["dropped"] >= 15
    assert all(s["truncated"] for s in sel["selected"])      # each 50k file head-truncated to 10k


def test_pin_predicate_for_terraform_core():
    entries = _entries(("main.tf", "resource"), ("src/app.py", "code"))
    sel = rr.select_files(entries, "python change", ui=False, pin=lambda r: r.endswith(".tf"))
    pinned = {s["rel"] for s in sel["selected"] if s["pinned"]}
    assert "main.tf" in pinned


def test_render_context_labels_design_files():
    sel = rr.select_files(_entries(("tailwind.config.ts", "theme"), ("a.py", "code")),
                          "task", ui=True)
    out = rr.render_context(["tailwind.config.ts", "a.py"], sel)
    assert "REPO FILES:" in out and "(design)" in out and "tailwind.config.ts" in out


# --- integration: the refactored _repo_context wiring ------------------------
def test_repo_context_import_smoke():
    # the refactor imports cleanly and threads task through
    from apps.api.app.services import code_agent
    assert hasattr(code_agent, "repo_retriever")
