"""ADD A GITHUB ACCOUNT, as a person does it (BOSSAPP-945, 1015, 1016), at 1440 and 390, against
a fake GitHub.

  1 the whole way for an organisation: found on GitHub → the walled app route (direct connection
    primary, the owner's switch behind a note, no "I did it") → the app made public on GitHub,
    noticed by BOSS → Install aimed at the account → the installation appearing on GitHub,
    noticed by BOSS → Pick repos with "used by" → Allow, the access check's results in words;
  2 a name GitHub does not know, said in words;
  3 an account already installed: straight to Pick repos;
  4 a person, with the app public: Install through GitHub's own sign-in, said plainly before;
  5 a foreign person on a private app: the direct connection, never a dead Install button;
  6 the app's owner: the app route, unchanged;
  7 Other ways: the token (with its exact permissions), the org's own app.
"""
from __future__ import annotations

from conftest import github  # noqa: F401  (tests/e2e/add_github_account/conftest.py)

SLUG = "boss-localhost-ab12"


def test_an_organisation_from_name_to_allowed_repositories(person):
    person.open_sheet()
    person.shot("0-empty")
    person.look_up("kaappu")
    found = person.tid("ga-found").inner_text()
    assert "kaappu" in found and "organisation" in found and "14 public repositories" in found, found
    assert person.path() == "make_public"
    assert "Agent actions" in person.tid("ga-agent-actions").inner_text()
    # The app route is walled (private app, foreign account): the direct connection leads, the
    # owner's one-time switch sits behind a note, and there is NO Install button to a 404.
    assert person.tid("ga-install").count() == 0
    assert person.tid("ga-connect-direct").inner_text() == "Paste a token for kaappu"
    assert "bare 404" in person.tid("ga-gate-private").inner_text()
    person.tid("ga-owner-note").locator("summary").click()
    settings = person.tid("ga-open-settings")
    assert settings.get_attribute("href") == f"https://github.com/settings/apps/{SLUG}/advanced"
    assert settings.get_attribute("target") == "_blank"
    sheet = person.tid("ga-sheet").inner_text()
    assert "I did it" not in sheet
    person.check("the walled app route")
    person.shot("1-walled-app-route")

    github(public=True)                                   # the owner pressed Make public on GitHub
    took = person.wait_path("install")
    assert took < 30, f"BOSS took {took:.0f} s to notice the app was made public"
    install = person.tid("ga-install")
    assert install.get_attribute("href") == f"https://github.com/apps/{SLUG}/installations/new/permissions?target_id=7202"
    assert install.get_attribute("target") == "_blank"
    assert "needs one of its admins" in person.tid("ga-install-note").inner_text()
    assert "Switch account" in person.tid("ga-gate-install").inner_text()
    assert person.tid("ga-step-gate").get_attribute("data-s") == "done"
    person.check("install")
    person.shot("2-install")

    state = github(install="kaappu")                      # …then pressed Install on kaappu
    iid = next(i for i, who in state["installations"].items() if who == "kaappu")
    took = person.wait_path("pick")
    assert took < 30, f"BOSS took {took:.0f} s to notice the installation"
    person.p.wait_for_selector("[data-testid='ga-repo-kaappu/helpdesk']")
    assert person.tid("ga-used-kaappu/website").count() == 0
    assert person.tid("ga-installation-settings").get_attribute("href") == (
        f"https://github.com/organizations/kaappu/settings/installations/{iid}")
    person.check("pick repos")
    person.shot("3-pick-repos")

    github(read_only=["kaappu/website"])
    person.tid("ga-pick-kaappu/helpdesk").check()
    person.tid("ga-pick-kaappu/website").check()
    assert person.tid("ga-allow").inner_text() == "Allow 2 repositories"
    person.tid("ga-allow").click()
    person.p.wait_for_selector("[data-testid='ga-done']", timeout=60_000)
    assert person.tid("ga-result-kaappu/helpdesk").inner_text().startswith("Working:")
    assert person.tid("ga-result-kaappu/website").inner_text().startswith("Read only:")
    assert person.tid("ga-step-check").get_attribute("data-s") == "done"
    assert person.tid("ga-foot-note").inner_text() == "1 of 2 working."
    person.check("allowed")
    person.shot("4-allowed")
    person.tid("ga-done").click()
    person.tid("su-add-account").wait_for(state="detached")


def test_a_name_github_does_not_know_is_said_in_words(person):
    person.expect(r"^404 POST /v1/identity/github/accounts/lookup")
    person.open_sheet()
    person.look_up("nobody-here")
    assert person.tid("ga-error").inner_text() == "No GitHub account called nobody-here. Check the spelling."
    assert person.tid("ga-agent-actions").count() == 0
    person.check("not found")
    person.shot("5-not-found")


def test_an_account_already_installed_goes_straight_to_pick_repos(person):
    github(install="kaappu")
    person.open_sheet()
    person.look_up("https://github.com/kaappu/helpdesk")
    assert person.path() == "pick"
    assert person.tid("ga-step-gate").count() == 0
    person.p.wait_for_selector("[data-testid='ga-repo-kaappu/infra-scripts']")
    person.check("already installed")
    person.shot("6-already-installed")


def test_a_person_with_the_app_public_installs_through_githubs_own_sign_in(person):
    github(public=True)
    person.open_sheet()
    person.look_up("octo-person")
    assert "person" in person.tid("ga-found").inner_text()
    assert person.path() == "install"
    # Said plainly BEFORE the button, and the link goes through github.com/login (a wrong session
    # gets a bare 404 from GitHub on a person's install page).
    assert "signed into GitHub as octo-person" in person.tid("ga-install-note").inner_text()
    href = person.tid("ga-install").get_attribute("href")
    assert href.startswith("https://github.com/login?return_to=")
    assert "target_id%3D8303" in href
    assert person.tid("ga-gate-private").count() == 0
    person.check("a person")
    person.shot("7-person-install")


def test_a_foreign_person_on_a_private_app_gets_the_direct_route(person):
    person.open_sheet()
    person.look_up("octo-person")
    assert person.path() == "make_public"
    assert person.tid("ga-install").count() == 0          # never a dead Install button (a 404)
    direct = person.tid("ga-connect-direct")
    assert direct.inner_text() == "Paste a token for octo-person"
    person.tid("ga-owner-note").locator("summary").click()         # the owner's note, expanded
    assert "Make public" in person.tid("ga-owner-note").inner_text()
    person.check("the direct route")
    person.shot("10-foreign-person-private-app")
    direct.click()                                        # straight into the token way, prefilled
    person.p.wait_for_selector("[data-testid='su-how-token-form']")
    assert "target_name=octo-person" in person.tid("su-account-create-token").get_attribute("href")
    person.shot("11-token-prefilled")


def test_the_apps_owner_keeps_the_app_route(person):
    state = github()                                       # the owner's own installation, removed on GitHub
    for iid in list(state["installations"]):
        github(uninstall=iid)
    person.open_sheet()
    person.look_up("paramg")
    assert person.path() == "install"
    note = person.tid("ga-install-note").inner_text()
    assert "signed into GitHub as paramg" in note          # the owner is a person too
    assert person.tid("ga-install").get_attribute("href").startswith("https://github.com/login?return_to=")
    person.check("the owner")
    person.shot("12-owner-install")


def test_other_ways_keep_the_token_and_the_orgs_own_app(person):
    person.open_sheet()
    person.look_up("kaappu")
    person.tid("ga-other").locator("summary").click()
    assert person.tid("ga-other-own-app").is_visible()
    person.shot("8-other-ways")
    person.tid("ga-other-token").click()
    person.p.wait_for_selector("[data-testid='su-how-token-form']")
    perms = person.tid("su-account-token-perms").inner_text()
    assert "Contents: Read and write" in perms and "Pull requests: Read and write" in perms
    href = person.tid("su-account-create-token").get_attribute("href")
    assert href.startswith("https://github.com/settings/personal-access-tokens/new?") and "contents=write" in href
    assert "target_name=kaappu" in href and "pull_requests=write" in href
    person.check("token")
    person.shot("9-token")
    person.tid("su-account-back").click()
    person.p.wait_for_selector("[data-testid='ga-input']")
