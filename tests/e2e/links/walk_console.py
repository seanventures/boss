"""Walk App ▸ Links in a real browser as the seeded Operator (after proof.py --keep, on a claimed
stack with seed-test-accounts written to <dir>/accounts.json):

    python3 tests/e2e/links/walk_console.py <dir>

Factory ▸ Apps ▸ Bank attacker ⋯ ▸ Links ▸ + Link (Banking, BANK, Preview, Test users) ▸ Link.
Screenshots land in <dir>."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.e2e.harness import identity as I
from playwright.sync_api import sync_playwright
S = Path(sys.argv[1]); origin = "http://localhost:58501"
acct = I.load_accounts(S / "accounts.json")["operator"]
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1400, "height": 900}, color_scheme="dark")
    I.sign_in(page, origin, acct, next_path="/?area=factory")
    page.goto(origin + "/?area=factory", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    page.screenshot(path=str(S / "w1-factory.png"))
    tab = page.get_by_role("tab", name="Apps")
    if tab.count(): tab.first.click()
    else:
        page.get_by_text("Apps", exact=True).first.click()
    page.wait_for_timeout(3000)
    page.screenshot(path=str(S / "w2-apps.png"))
    row = page.locator(".fx-app", has_text="Bank attacker").first
    row.hover()
    row.locator(".fx-app-more").click()
    page.locator("[data-testid^='fx-app-menu-links-']").first.click()
    page.wait_for_selector("[data-testid='fx-links-sheet']")
    page.wait_for_timeout(1500)
    page.screenshot(path=str(S / "w3-links.png"))
    page.click("[data-testid='fx-link-add']")
    page.select_option("[data-testid='fx-link-provider']", label="Banking")
    page.fill("[data-testid='fx-link-alias']", "BANK")
    page.click("[data-testid='fx-link-submit']")
    page.wait_for_selector("[data-testid='fx-link-BANK']", timeout=15000)
    page.wait_for_timeout(1000)
    page.locator("[data-testid='fx-links-sheet']").screenshot(path=str(S / "w4-linked.png"))
    page.screenshot(path=str(S / "w4-page.png"))
    print("texts:", page.locator("[data-testid='fx-links-sheet']").inner_text()[:600])
    b.close()
