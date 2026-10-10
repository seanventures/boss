"""The multi-repo screens, captured from the proof's stack with its real data (proof.py `shots`).

Run after `proof.py run` on the same stack: the registry, the project's repositories and the
components already exist, the first change is live and the second blocked. This walk stops the
blocked one and starts a THIRD change from the app's "New change" in the console, so the consent
sheet, the cockpit while building, the linked PRs and the approval card are all real, then shows
the first change's deploy plan. PNGs go to docs/product/multirepo/ (descriptive names).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from e2e.identity import kit as K
from e2e.multirepo import proof as P

OUT = Path(__file__).resolve().parents[3] / "docs" / "product" / "multirepo"


class Shots(P.Proof):
    def snap(self, name: str, selector: str = "", timeout: int = 30000) -> None:
        page = self.pg
        try:
            if selector:
                page.wait_for_selector(selector, timeout=timeout)
            page.wait_for_timeout(1500)
            vis = page.evaluate("document.visibilityState")
            path = OUT / f"{name}.png"
            page.screenshot(path=str(path), full_page=True)
            self.saved.append(path.name)
            self.j.note(f"shot {path.name} (visibility {vis})")
        except Exception as exc:  # noqa: BLE001
            self.j.check(f"screen {name} renders", False, expect=selector or "page", actual=f"{type(exc).__name__}: {str(exc)[:200]}")
            try:
                page.screenshot(path=str(OUT / f"{name}--FAILED.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass

    def go(self, q: str) -> None:
        self.pg.goto(f"{self.web}/{q}", wait_until="domcontentloaded", timeout=90000)
        K.settle(self.pg)

    def capture_all(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.saved: list[str] = []
        ctx, page = self.page()
        self.pg = page
        try:
            self.project = self.psql("SELECT id FROM projects ORDER BY created_at LIMIT 1")
            self.app_key = self.psql("SELECT app_key FROM app_components WHERE project_id = :'p' LIMIT 1", p=self.project)
            happy = self.psql("SELECT id FROM repo_change_sets WHERE mode='multi' AND app_key = :'a' AND state IN "
                              "('live','merged','deploying','rolled_back') ORDER BY created_at LIMIT 1", a=self.app_key)
            blocked = self.psql("SELECT id FROM repo_change_sets WHERE mode='multi' AND state IN ('blocked','building',"
                                "'in_review','planned') ORDER BY created_at DESC LIMIT 1")
            self.go("?area=setup&focus=repositories")
            self.snap("01-registry-repo-and-models", "[data-testid=mr-registry]")
            self.go("?area=code_tasks&view=repositories")
            self.snap("02-project-repositories", "text=acme/web")
            self.go("?area=workflows")
            page.wait_for_selector("[data-testid=dw-composer-open], [data-testid=np-mode]", timeout=90000)
            if page.locator("[data-testid=dw-composer-open]").count():
                page.locator("[data-testid=dw-composer-open]").first.click()
            page.click("[data-testid=np-mode-new]", timeout=30000)
            page.locator("[data-testid=np-layout-several]").first.click(timeout=30000)
            page.wait_for_timeout(2500)
            self.snap("03-new-project-several-repos", "[data-testid=np-layout]")
            self.go(f"?area=code_tasks&view=components&app={self.app_key}")
            self.snap("04-app-components", "[data-testid=mr-cmp-flow]")
            if blocked:
                self.call("POST", f"/v1/change-sets/{blocked}/stop", {"project_id": self.project})
            # "New change" from the app: the consent sheet asks for exactly the app's repositories
            self.go(f"?area=code_tasks&view=components&app={self.app_key}")
            page.click("[data-testid=mr-cmp-new]", timeout=30000)
            page.fill("[data-testid=mr-new-change-title]", "Add saved carts")
            what = page.locator("[data-testid=mr-new-change-what]")
            if what.count():
                what.fill("Shoppers can save a cart and come back to it: contract, API and web.")
            page.click("[data-testid=mr-new-change-start]")
            page.wait_for_selector("[data-testid=consent-sheet]", timeout=30000)
            page.wait_for_timeout(1200)
            self.snap("09-consent-sheet-three-repositories", "[data-testid=consent-sheet]")
            page.locator("[data-testid=consent-durations] button").first.click()
            page.wait_for_timeout(2500)
            if K.code_box(page).count():
                K.answer_step_up_sheet(page, self.owner)
            cs = K.wait_for(lambda: self.psql("SELECT id FROM repo_change_sets WHERE title ILIKE '%saved carts%' "
                                              "ORDER BY created_at DESC LIMIT 1") or None, timeout_s=90, every_s=3)
            if not cs:
                raise RuntimeError("the console's New change did not start a change")
            self.state["shots_change"] = cs
            # the cockpit while the lanes build
            self.wait_set(cs, lambda v: any(lane.get("lane_state") == "ready" for lane in v.get("lanes") or [])
                          or v.get("state") in ("blocked", "in_review"), 900, "shots")
            self.go(f"?area=code_tasks&changeset={cs}")
            self.snap("05-multi-repo-build-cockpit", "text=Saved carts")
            v = self.wait_set(cs, lambda v: v.get("state") in ("in_review", "blocked", "failed"), self.args.build_timeout, "shots")
            self.go(f"?area=code_tasks&changeset={cs}&tab=prs")
            self.snap("06-linked-prs", "text=acme/web")
            self.go("?area=decisions")
            self.snap("07-approval-card-one-decision", "[data-testid=mr-approval]")
            if happy:
                self.go(f"?area=infra&tab=deploys&changeset={happy}")
                self.snap("08-deploy-plan", "text=acme/api")
            self.state["shots_final"] = v.get("state")
        except Exception as exc:  # noqa: BLE001
            self.j.error = K.fmt_exc(exc)
            self.j.note(f"shots ABORTED: {self.j.error}")
        finally:
            ctx.close()


def capture(args: argparse.Namespace, st: Any) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy={"server": st.fake, "bypass": "localhost,127.0.0.1"})
        s = Shots(P._args_for_walk(args, st), browser, st)
        s.capture_all()
        browser.close()
    result = {"saved": s.saved, "checks": s.j.checks, "error": s.j.error, "notes": s.j.notes[-30:]}
    K.write_private(st.dir / "shots.json", json.dumps(result, indent=1))
    print(f"[multirepo shots] {len(s.saved)} saved to {OUT}: {s.saved}; error: {s.j.error}")
    return 0 if not s.j.error and not any(c["status"] == "fail" for c in s.j.checks) else 1


if __name__ == "__main__":
    raise SystemExit("run through proof.py shots")
