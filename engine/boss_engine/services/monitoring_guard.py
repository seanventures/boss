"""A CHANGE NEVER SILENTLY DROPS AN APP'S MONITORING.

PR paramg/boss-sandbox#234 (run wfr_c2f28f2f, "Fix Order API: KeyError on POST /orders") ended
with a commit that replaced the app's 259-line boss.deploy.yaml — Beacon's monitoring pack:
`alerts:` with eight rules, `dashboard:`, runbook links — by the 15-line manifest from before the
pack, and that manifest was deployed. Nobody asked for it and nothing caught it. The mechanism:
the app had a test (tests/test_deploy_manifest.py, written by an earlier build) pinning the
manifest's exact top-level keys; the pack added `alerts:` and `dashboard:`, so that test failed;
an implementer may not edit tests (they are frozen), so it "fixed" the failure by writing the
manifest the test pinned — deleting the pack.

Three guards, each at the place every path goes through:

- `guard_files` — at the pull request (software_factory.propose_pull_request, which every BOSS
  PR is opened by): a manifest whose `alerts:` / `dashboard:` entries were removed gets those
  sections back from the branch's own version, unless the change declared exactly that removal
  (`allowed`: a monitoring change names what it removes). The rest of the change stands; the PR
  says what was kept and why.
- `KEEP_MONITORING_RULE` — in every prompt that writes code or tests: the pack is not the
  change's to remove, and a test about the manifest checks what it cares about, never the exact
  key set.
- app_alerts.ensure_rules — a deploy whose manifest lost rules keeps them running, marks them
  and raises an alert (`MonitoringRulesDropped`), instead of letting them drift silently.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

MANIFEST_NAME = "boss.deploy.yaml"
#: The top-level sections of boss.deploy.yaml that Set up monitoring owns.
MONITORING_KEYS = ("alerts", "dashboard")

KEEP_MONITORING_RULE = (
    "- The app's monitoring lives in boss.deploy.yaml under `alerts:` and `dashboard:` (written "
    "by BOSS's Set up monitoring step, with runbooks/). It is not this change's to remove or "
    "rewrite: never delete or trim those sections, a rule in them, or a runbook. If an existing "
    "test fails only because it pins the manifest's exact shape (its full key set, or no "
    "`alerts:`), say so in your summary instead of changing the manifest to match it. A test "
    "you write about boss.deploy.yaml checks the keys it cares about and allows `alerts:` and "
    "`dashboard:`.\n")


def is_manifest(path: str) -> bool:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1] == MANIFEST_NAME


def entries(text: Optional[str]) -> Optional[set[str]]:
    """Every monitoring entry the manifest declares, named the way a person reads them:
    `alerts.<built-in>`, `rule <AlertName>`, `panel <title>`. None when it does not parse."""
    import yaml

    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    out: set[str] = set()
    alerts = doc.get("alerts") if isinstance(doc.get("alerts"), dict) else {}
    for key, spec in alerts.items():
        if key == "rules":
            for rule in spec if isinstance(spec, list) else []:
                if isinstance(rule, dict) and rule.get("alert"):
                    out.add(f"rule {rule['alert']}")
        else:
            out.add(f"alerts.{key}")
    dash = doc.get("dashboard") if isinstance(doc.get("dashboard"), dict) else {}
    for panel in dash.get("panels") or []:
        if isinstance(panel, dict) and panel.get("title"):
            out.add(f"panel {panel['title']}")
    return out


def removed(before: Optional[str], after: Optional[str]) -> list[str]:
    """The monitoring entries `before` had and `after` does not ([] when either is unreadable:
    an unparsable manifest is refused by the deploy's own parser, not here)."""
    was, now = entries(before), entries(after)
    if was is None or now is None:
        return []
    return sorted(was - now)


def block(text: str, key: str) -> Optional[str]:
    """The top-level `key:` block of a yaml text, verbatim (comments kept), or None."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", line)),
                 None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end])


def restore(before: str, after: str) -> str:
    """`after` with every monitoring section `before` had put back verbatim."""
    from .monitoring_pack import replace_block

    out = after
    for key in MONITORING_KEYS:
        was = block(before, key)
        if was is not None:
            out = replace_block(out, key, was)
    return out


def guard_files(files: Iterable[Any], read_before: Callable[[str], Optional[str]], *,
                allowed: Optional[Iterable[str]] = None) -> tuple[list[Any], list[dict]]:
    """The change's files with any undeclared monitoring removal undone.

    `files` are FileChange-like (`.path`, `.content`, `model_copy`); `read_before(path)` is the
    manifest the change starts from (the branch's own version). `allowed`: the entries a
    monitoring change names as removed on purpose. Returns (files, kept) where `kept` says, per
    manifest, which entries were put back."""
    ok = {str(a) for a in (allowed or ())}
    out: list[Any] = []
    kept: list[dict] = []
    for f in files:
        if not is_manifest(getattr(f, "path", "")) or not isinstance(
                getattr(f, "content", None), str):
            out.append(f)
            continue
        try:
            before = read_before(f.path)
        except Exception:  # noqa: BLE001 — cannot compare: the manifest's own parser still runs
            before = None
        lost = [e for e in removed(before, f.content) if e not in ok]
        if not before or not lost:
            out.append(f)
            continue
        fixed = restore(before, f.content)
        still = [e for e in removed(before, fixed) if e not in ok]
        if still:
            # the sections moved somewhere restore cannot reach: keep the branch's file whole
            fixed = before
        kept.append({"path": f.path, "removed": lost})
        if fixed != before:
            out.append(f.model_copy(update={"content": fixed}) if hasattr(f, "model_copy")
                       else type(f)(path=f.path, content=fixed))
    return out, kept


def pr_section(kept: list[dict]) -> str:
    """What the pull request says when the guard put monitoring back."""
    if not kept:
        return ""
    rows = []
    for k in kept:
        names = ", ".join(k["removed"][:12]) + (
            f" and {len(k['removed']) - 12} more" if len(k["removed"]) > 12 else "")
        rows.append(f"- `{k['path']}`: {names}")
    return ("## Monitoring kept\n\nThis change removed monitoring it was not asked to touch, so "
            "BOSS put it back (the rest of the change is as written):\n" + "\n".join(rows)
            + "\n\nIf a test failed because it pins the manifest's exact shape, the test is what "
            "needs changing: it should allow `alerts:` and `dashboard:`.\n")
