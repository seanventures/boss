"""Every workflow step has a named SME who answers for it.

"Instead of Ask BOSS about this step we should have a captain assigned who can answer those
questions … let's make sure we are assigning a proper Agent SME for each step."

The role-addressed chat path already existed and is strictly better than asking BOSS in general:
it reads the role's own definition and the positions that role has actually taken on this
project. What it never had was anything saying that a deploy belongs to the Deployment Manager
and a security review to the Security Reviewer.
"""
from boss_engine.services.workflow_capabilities import (
    CAPABILITIES,
    REGISTERED_ROLES,
    palette,
)

#: IMPORTED, NOT RESTATED. This list used to be copied here, so adding a capability with a new
#: captain failed on the copy rather than on anything real — the service knew the role, the test
#: did not, and the "fix" was to paste a name into a second place. The codebase's own rule
#: (`MANUAL_APPROVAL_RISK_FLOOR` is imported "so the two cannot drift") applies to it too.
#:
#: What this still catches is the thing worth catching: a captain that is a typo, or a role
#: nobody registered. Adding one is now a deliberate edit to REGISTERED_ROLES, in front of a
#: reviewer, rather than an edit to a test fixture.
REGISTERED = REGISTERED_ROLES


def test_no_step_is_without_an_owner():
    orphans = sorted(k for k, c in CAPABILITIES.items() if not c.captain)
    assert not orphans, f"these steps have nobody to ask: {orphans}"


def test_every_captain_is_a_registered_role():
    unknown = sorted({c.captain for c in CAPABILITIES.values()} - REGISTERED)
    assert not unknown, f"not roles this workforce has: {unknown}"


def test_the_assignment_is_by_subject_matter():
    """One captain may hold several steps — that says something true about the workforce — but
    the lens has to match the work."""
    assert CAPABILITIES["security_review"].captain == "security_reviewer"
    assert CAPABILITIES["ux_review"].captain == "ux_engineer"
    assert CAPABILITIES["deploy"].captain == "deployment_manager"
    assert CAPABILITIES["cost_check"].captain == "finops_sentinel"
    assert CAPABILITIES["test"].captain == "qa"
    assert CAPABILITIES["monitor"].captain == "infra_steward"


def test_a_repeat_of_the_same_work_keeps_the_same_hands():
    assert CAPABILITIES["redeploy"].captain == CAPABILITIES["deploy"].captain
    assert CAPABILITIES["retry_build"].captain == CAPABILITIES["build"].captain


def test_it_reaches_the_console_through_the_capability():
    """Not a second copy in the console, which would drift the first time a step was added."""
    entries = {c["key"]: c for c in palette()}
    assert entries["deploy"]["captain"] == "deployment_manager"
    assert all(c["captain"] for c in entries.values())


def test_a_captain_is_a_role_a_project_can_actually_install():
    """A registered captain with no spec and no pack entry addresses an empty chair.

    `REGISTERED_ROLES` is what `validate_captain` checks against, but a project gets its roles
    from a PACK. A name in one and not the other passes every check here and then fails for the
    operator, who clicks the captain and reaches a role with no definition and no record.
    """
    import pathlib
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "specpack"
    installed: set[str] = set()
    for pack in (root / "packs").glob("*.yaml"):
        doc = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
        installed |= set((doc.get("installs") or {}).get("roles") or [])

    #: KNOWN, PRE-EXISTING, AND DOCUMENTED ELSEWHERE. These four run work through the agent
    #: process template while being installed by no pack — the same gap
    #: `console_v2_workforce.py` describes as "agents that work but are not registered roles",
    #: where it notes the consequence: no trust score, no autonomy level, no violation
    #: tracking. Listed here so the assertion below still bites for anything NEW, rather than
    #: being deleted because it found something true and inconvenient.
    KNOWN_UNROOTED = {
        "chief_architect", "engineering_captain", "product_manager", "ux_engineer",
    }

    captains = {c.captain for c in CAPABILITIES.values() if c.captain}
    missing = sorted(captains - installed - KNOWN_UNROOTED)
    assert not missing, (
        f"these captains are registered but no pack installs them: {missing}. "
        "Add the role to a pack's installs.roles, or a project will never have it."
    )
    # And the list must not grow silently: if one of these is fixed, delete it from the set
    # rather than leaving a stale exemption that hides the next one.
    still_unrooted = KNOWN_UNROOTED - installed
    assert still_unrooted == KNOWN_UNROOTED, (
        f"these are now installed and can be removed from KNOWN_UNROOTED: "
        f"{sorted(KNOWN_UNROOTED - still_unrooted)}"
    )


def test_publishing_belongs_to_the_media_publisher_and_forces_an_approval():
    """Publishing is irreversible and public, which fixes both of these.

    The tier is not a style choice: you cannot un-publish something people have already seen,
    so it sits at the tier that requires a person to agree. The captain is the role whose remit
    is the audience — not whichever step happened to need the credential.
    """
    from boss_engine.services.workflow_capabilities import MANUAL_APPROVAL_RISK_FLOOR

    publish = CAPABILITIES["social_publish"]
    assert publish.captain == "media_publisher"
    assert publish.risk_tier >= MANUAL_APPROVAL_RISK_FLOOR
    # No built-in tool: there is no generic "post to the internet". A step of this kind is
    # always bound to a skill holding one account's credential.
    assert publish.tool is None
    # The inputs are platform-agnostic on purpose, so a second platform is a new binding
    # rather than a new capability.
    assert "media" in publish.required_inputs and "title" in publish.required_inputs
    assert "post_url" in publish.produces
