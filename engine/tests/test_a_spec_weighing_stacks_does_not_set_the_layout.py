"""A product spec that weighs stacks is not a request for one (crun_db8299feecc449fcbe60).

HelpDesk AI (an Express + Vite app) was asked for SLA timers. Its PRD told the builder to
"confirm whether the backend is Python (FastAPI/...) or Node (... Next.js)". The layout read the
spec for language names and demanded a Python backend in backend/ and a Next front end in
frontend/, failing a change whose 133 tests passed. The directive is the request; the spec is not.
"""
from boss_engine.services import stacks

BRIEF = """Add SLA timers to tickets: each priority gets a response deadline. Show a countdown chip.

The work happens in the repository paramg/helpdesk-ai. If it already holds an app, this is a change to that EXISTING codebase.

---

THE PRODUCT SPEC BELOW IS THE BRIEF.

# PRD: SLA Timers on Tickets

## 11. Open Questions
2. **Stack confirmation:** Confirm whether `helpdesk-ai`'s backend is Python (FastAPI/Flask/Django) or Node (Express/Fastify/Koa/Hono/NestJS/Next.js), and build strictly within that existing stack.
"""


def test_the_spec_weighing_python_against_node_sets_no_layout():
    assert stacks.build_layout(BRIEF, "Add SLA timers to tickets") is None


def test_a_layout_the_operator_asks_for_still_holds():
    asked = "A FastAPI backend in backend/ and a React front end in frontend/.\n\n---\n\nTHE PRODUCT SPEC BELOW."
    layout = stacks.build_layout(asked)
    assert layout is not None and (layout.backend_language, layout.backend_dir, layout.frontend_dir) == \
        ("python", "backend", "frontend")


def test_the_layout_and_the_build_guard_read_the_same_words():
    assert stacks.directive_text(BRIEF).startswith("Add SLA timers")
    assert "Stack confirmation" not in stacks.directive_text(BRIEF)
