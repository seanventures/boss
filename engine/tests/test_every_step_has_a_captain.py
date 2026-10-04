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
        # chief_architect and engineering_captain left this list when the Agent World cast
        # shipped their role YAMLs (core_v1/core_v2).
        "product_manager", "ux_engineer",
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
    # `media_artifact_id`, matching what an editing step PRODUCES — a publish step requiring
    # `media` wired to the run's input instead of to the video just edited.
    assert "media_artifact_id" in publish.required_inputs
    assert "title" in publish.required_inputs
    assert "post_url" in publish.produces


def test_every_role_spec_in_the_pack_actually_parses_as_intended():
    """An unquoted colon turns a YAML list entry into a dict.

    `- Pre-publish checks: the media plays, ...` parses as {"Pre-publish checks": "the media
    plays, ..."} — not as the sentence it obviously reads as. The installer rejects it with
    "primary_outcomes must contain only non-empty strings", which names the symptom and not the
    colon, and it does so on the deployment rather than here. Caught at the pack instead.
    """
    import pathlib
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "specpack" / "roles"
    offenders: list[str] = []
    for spec in sorted(root.glob("*.yaml")):
        doc = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        lists = {
            "primary_outcomes": doc.get("primary_outcomes") or [],
            "output_contracts": doc.get("output_contracts") or [],
            "must": (doc.get("boundaries") or {}).get("must") or [],
            "must_not": (doc.get("boundaries") or {}).get("must_not") or [],
        }
        for field, entries in lists.items():
            for entry in entries:
                if not isinstance(entry, str) or not entry.strip():
                    offenders.append(f"{spec.name}:{field} -> {entry!r}")
    assert not offenders, (
        "these list entries are not strings — usually an unquoted colon making YAML read a "
        f"sentence as a mapping: {offenders}"
    )


def test_every_skill_file_is_reachable_from_some_edition():
    """A skill nobody can see is a skill that does not exist.

    `list_skills` filters by BUNDLE — the skill file's stem — against the set an edition's pack
    reaches through its `requires` chain. A skill file that no pack lists loads fine, validates
    fine, registers its executor fine, and is then absent from every catalogue, so assigning it
    to an agent fails with "no skill in this project".

    That cost a real debugging pass: `publish_youtube` appeared to assign successfully, but
    only because a project-authored skill of the same name shadowed it. The built-in was
    invisible the whole time.
    """
    import json
    import pathlib
    import yaml

    repo = pathlib.Path(__file__).resolve().parents[2]
    root = repo / "packages" / "specpack"

    def bundles_of(pack_id: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        if pack_id in seen:
            return set()
        seen.add(pack_id)
        path = root / "packs" / f"{pack_id}.yaml"
        if not path.exists():
            return set()
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out = {str(s) for s in ((doc.get("installs") or {}).get("skills") or [])}
        for req in (doc.get("requires") or []):
            out |= bundles_of(str(req), seen)
        return out

    reachable: set[str] = set()
    for pack in (root / "packs").glob("*.yaml"):
        reachable |= bundles_of(pack.stem)

    # The private source tree also holds reviewed Community variants of two installed bundles.
    # Export replaces github.yaml and software_factory.yaml with these files and removes their
    # *.community source names; installing the variants as extra bundles here would expose
    # duplicate skill ids instead of fixing reachability.
    export_sources: set[str] = set()
    export_manifest = repo / "scripts" / "community_export_manifest.json"
    if export_manifest.is_file():
        # The export tool and its manifest do not ship in the public tree. There, the reviewed
        # source variants have already replaced their destinations and no exception is needed.
        manifest = json.loads(export_manifest.read_text())
        for rewrite in manifest.get("specpack_export_rewrites", []):
            source = pathlib.Path(rewrite["source"])
            destination = pathlib.Path(rewrite["destination"])
            if source.parent != pathlib.Path("packages/specpack/skills"):
                continue
            assert destination.parent == source.parent
            assert destination.stem in reachable, f"export target {destination} is not installed"
            assert source.stem not in reachable, f"export source {source} is installed twice"
            if (repo / source).is_file():
                export_sources.add(source.stem)

    #: KNOWN AND PRE-EXISTING. Three skill bundles ship in the repo and are in no pack, so no
    #: edition can see them. Listed rather than deleted from the assertion, because the
    #: assertion found them and that is what it is for — and because each is a decision
    #: somebody should make deliberately (ship it, or remove the file) rather than a bug to
    #: paper over. Fixing one means deleting it from here.
    # `email` was adopted into community_edition once its executor existed — the exemption
    # list is not meant to outlive the reason for it.
    KNOWN_ORPHANS = {"agent_toolchain", "infra_do", "slack"}

    on_disk = {p.stem for p in (root / "skills").glob("*.yaml")}
    orphans = sorted(on_disk - reachable - KNOWN_ORPHANS - export_sources)
    assert not orphans, (
        f"these skill files are in no pack, so no edition's catalogue includes them: {orphans}. "
        "Add the bundle to a pack's installs.skills — community_edition if every edition "
        "should have it, since enterprise and cloud both require it."
    )
    # And the exemption list must not go stale: an orphan that gets adopted should be removed
    # from it, not left as cover for the next one.
    adopted = sorted(KNOWN_ORPHANS & reachable)
    assert not adopted, f"these are now in a pack and can leave KNOWN_ORPHANS: {adopted}"


def test_community_catalog_exposes_the_opt_in_mobile_skill_once():
    """Pack reachability must match the project catalog the operator can actually assign."""
    import pathlib
    from types import SimpleNamespace

    from boss_engine.config import get_settings
    from boss_engine.services.entitlements import EntitlementBrokerService

    class EmptyProjectSkills:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))

    root = pathlib.Path(__file__).resolve().parents[2] / "packages" / "specpack"
    settings = get_settings().model_copy(update={
        "boss_edition": "community", "specpack_root": root,
    })
    skills = EntitlementBrokerService(settings).list_skills(
        "project", connection=EmptyProjectSkills()
    )
    mobile = [skill for skill in skills if skill["id"] == "mobile.device.execute"]
    assert len(mobile) == 1
    assert mobile[0]["on_demand"] is True
    assert mobile[0]["policy"]["requires_lease"] is True
    # Source-only export variants must not duplicate the installed monorepo bundles.
    assert sum(skill["id"] == "github.create_pr_real" for skill in skills) == 1
    assert sum(skill["id"] == "software_factory.codex_pr_proposal" for skill in skills) == 1
