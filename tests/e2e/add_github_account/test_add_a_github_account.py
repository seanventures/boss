"""ADD A GITHUB ACCOUNT, as a person does it (BOSSAPP-945), at 1440 and 390, against a fake GitHub.

  1 the whole way for an organisation: found on GitHub → the one-time switch (no "I did it") →
    the app made public on GitHub, noticed by BOSS → Install aimed at the account → the
    installation appearing on GitHub, noticed by BOSS → Pick repos with "used by" → Allow, the
    access check's results in words;
  2 a name GitHub does not know, said in words;
  3 an account already installed: straight to Pick repos;
  4 a person, with the app public: straight to Install, aimed at them;
  5 Other ways: the token (with its exact permissions), the org's own app.
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
    settings = person.tid("ga-open-settings")
    assert settings.get_attribute("href") == f"https://github.com/settings/apps/{SLUG}/advanced"
    assert settings.get_attribute("target") == "_blank"
    sheet = person.tid("ga-sheet").inner_text()
    assert "I did it" not in sheet
    assert person.tid("ga-waiting").is_disabled()
    person.check("the one-time switch")
    person.shot("1-let-it-install")

    github(public=True)                                   # the owner pressed Make public on GitHub
    took = person.wait_path("install")
    assert took < 30, f"BOSS took {took:.0f} s to notice the app was made public"
    install = person.tid("ga-install")
    assert install.get_attribute("href") == f"https://github.com/apps/{SLUG}/installations/new/permissions?target_id=7202"
    assert install.get_attribute("target") == "_blank"
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


def test_a_person_with_the_app_public_goes_straight_to_install(person):
    github(public=True)
    person.open_sheet()
    person.look_up("octo-person")
    assert "person" in person.tid("ga-found").inner_text()
    assert person.path() == "install"
    assert person.tid("ga-install").get_attribute("href").endswith("target_id=8303")
    assert person.tid("ga-gate-public").count() == 0
    person.check("a person")
    person.shot("7-person-install")


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
