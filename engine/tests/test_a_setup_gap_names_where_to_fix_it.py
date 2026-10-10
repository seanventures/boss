"""NO DEAD END: a step whose ability is not set up says WHERE it is set up (kit.setup_fix), the same
landings the start checklist uses (workflow_readiness.NEEDS). A deploy step with no target opens
Infra › Fleet with the App target sheet, not a page that only describes it."""
from __future__ import annotations

from boss_engine.services import kit
from boss_engine.services.workflow_readiness import NEEDS


def test_a_deploy_gap_opens_infra_with_the_target_sheet():
    assert NEEDS["deployment_target"].href == "/?area=infra&view=fleet&add=target"
    label, href = kit.setup_fix({"key": "deploy", "label": "Deploy", "depends_on": ["deploy_target"]},
                                "setup", {"deploy_target": False})
    assert (label, href) == ("Set up where it deploys", "/?area=infra&view=fleet&add=target")


def test_a_repo_gap_opens_the_repository_row_and_others_their_ability():
    assert kit.setup_fix({"key": "open_prs", "label": "Open pull requests", "depends_on": ["github"]},
                         "needs_repo", {"deploy_target": True}) == ("Connect a repository",
                                                                    "/?area=setup&focus=repository")
    assert kit.setup_fix({"key": "email", "label": "Email", "depends_on": []}, "setup",
                         {"deploy_target": True}) == ("Set up Email", "/?area=skills&ability=email")


def test_every_need_that_has_a_console_fix_links_to_a_real_area():
    import re
    from pathlib import Path

    shell = (Path(__file__).resolve().parents[2] / "apps/web/app/ConsoleShell.tsx").read_text()
    for need in NEEDS.values():
        if need.area:
            assert re.search(rf'["\']{need.area}["\']', shell), f"{need.key}: area {need.area} is not in the console"
