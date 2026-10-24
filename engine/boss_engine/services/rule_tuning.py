"""TUNE A RULE: change a noisy rule's threshold where the rule lives (P1-C).

A rule BOSS evaluates comes from one of two owners:

  * THE REPO — declared in the app's boss.deploy.yaml `alerts:` block (source `yaml`,
    managed_by `code`; app_alerts.ensure_rules). Tuning it writes the change into that file and
    PROPOSES it as a pull request: only that rule's lines change, everything else in the file —
    comments, order, the other rules and sections — stays byte for byte. The rule's row is not
    touched; the merged pull request's next deploy carries the new values (ensure_rules).
  * THE CONSOLE — BOSS's defaults, or a rule a person added or edited here. Tuning saves it
    directly through app_alerts.update_rule, which audits who, when, before and after.

Either way the person first sees the 24 h preview with the NEW values (`preview_change`):
"would have fired N times in the last 24 h", next to what the rule as it is would have done.
`where` says where a repo rule lives: the manifest's path and the line its rule starts on.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Optional

from ..ports import sql
from . import app_alerts as aa

#: Where a pull-request tuning's branch is cut (one per rule and change).
BRANCH_PREFIX = "boss/tune-"
#: The legacy spelling of the error-rate rule (metrics.alert) and its keys.
_LEGACY_KEYS = {"threshold": "error_rate_percent", "window_minutes": "window_minutes",
                "min_requests": "min_requests"}


class TuneRefused(ValueError):
    """A tuning BOSS will not make, in words (422)."""


def via(rule: dict[str, Any]) -> str:
    """`pull_request` for a rule the repo owns, `console` for every other."""
    return "pull_request" if (rule.get("source") == "yaml"
                              and (rule.get("managed_by") or "code") == "code") else "console"


# --------------------------------------------------------------------------- #
# Where the rule is in the manifest's text
# --------------------------------------------------------------------------- #
def _compose(text: str):
    import yaml

    try:
        return yaml.compose(text or "")
    except yaml.YAMLError as exc:
        raise TuneRefused(f"the manifest is not valid YAML: {exc}") from None


def _get(node, key: str):
    from yaml.nodes import MappingNode, ScalarNode

    if not isinstance(node, MappingNode):
        return None, None
    for k, v in node.value:
        if isinstance(k, ScalarNode) and k.value == key:
            return k, v
    return None, None


def _find(root, rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The rule's mapping node in the manifest and its key spelling, or None."""
    from yaml.nodes import MappingNode, SequenceNode

    _, alerts = _get(root, "alerts")
    if rule["kind"] == "custom":
        _, rules = _get(alerts, "rules")
        if isinstance(rules, SequenceNode):
            for item in rules.value:
                _, name = _get(item, "alert")
                if name is not None and str(name.value) == rule["alert"]:
                    return {"node": item, "start": item, "legacy": False}
        return None
    key, spec = _get(alerts, rule["kind"])
    if isinstance(spec, MappingNode):
        return {"node": spec, "start": key, "legacy": False}
    if rule["kind"] == "error_rate":
        _, metrics = _get(root, "metrics")
        key, spec = _get(metrics, "alert")
        if isinstance(spec, MappingNode):
            return {"node": spec, "start": key, "legacy": True}
    return None


def locate(text: str, rule: dict[str, Any]) -> Optional[dict[str, int]]:
    """{line, end_line} (1-based) of the rule in the manifest's text, or None."""
    got = _find(_compose(text), rule)
    if not got:
        return None
    end = _last_end(got["node"])
    last = end.line if end.column == 0 else end.line + 1
    return {"line": got["start"].start_mark.line + 1, "end_line": max(last, 1)}


def _last_end(node):
    """Where a block node's last value ends (a block's own end mark sits on the next key)."""
    from yaml.nodes import MappingNode, SequenceNode

    if isinstance(node, MappingNode) and node.value and not node.flow_style:
        return _last_end(node.value[-1][1])
    if isinstance(node, SequenceNode) and node.value and not node.flow_style:
        return _last_end(node.value[-1])
    return node.end_mark


def where(connection, target: dict[str, Any], rules: list[dict[str, Any]]
          ) -> dict[str, dict[str, Any]]:
    """rule id -> {path, line, end_line, repository, href} for every rule the repo owns, from the
    manifest the serving release was deployed with (its text is kept on the deploy)."""
    owned = [r for r in rules if via(r) == "pull_request"]
    if not owned:
        return {}
    row = connection.execute(sql(
        "SELECT repository, commit_sha, manifest_text, manifest->>'manifest_path' AS path "
        "FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
        "ORDER BY created_at DESC LIMIT 1"), {"t": target["id"]}).mappings().first()
    if not row or not row["manifest_text"]:
        return {}
    from .app_deploy_manifest import DEFAULT_MANIFEST_PATH

    path = str(row["path"] or DEFAULT_MANIFEST_PATH)
    out: dict[str, dict[str, Any]] = {}
    for r in owned:
        try:
            at = locate(row["manifest_text"], r)
        except TuneRefused:
            return {}
        if not at:
            continue
        href = None
        repo = str(row["repository"] or "")
        if "/" in repo:
            from ..identity.github_urls import web_base

            href = (f"{web_base()}/{repo}/blob/{row['commit_sha'] or 'HEAD'}/{path}"
                    f"#L{at['line']}-L{at['end_line']}")
        out[r["id"]] = {"path": path, **at, "repository": repo, "href": href}
    return out


# --------------------------------------------------------------------------- #
# The change, checked, as the rule's new values
# --------------------------------------------------------------------------- #
def checked(rule: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """The change in the yaml's own terms ({params?, for_seconds?, severity?, expr?}), each value
    checked with the bounds the console uses. Raises TuneRefused in words."""
    out: dict[str, Any] = {}
    try:
        if rule["kind"] == "custom":
            if "expr" in change:
                spec = aa.validate_custom({"alert": rule["alert"], "expr": change["expr"]})
                out["expr"] = spec["expr"]
        elif "params" in change:
            out["params"] = aa._bounded(rule["kind"], change["params"] or {})
        if "for" in change:
            out["for_seconds"] = aa._for_seconds(change["for"])
        if "severity" in change:
            if not aa.sev.accepted(change["severity"]):
                raise TuneRefused("severity must be sev1, sev2, sev3 or sev4")
            out["severity"] = str(change["severity"]).strip().lower()
    except (aa.RuleRefused, ValueError) as exc:
        raise TuneRefused(str(exc)) from None
    except aa.pq.PromQLError as exc:
        raise TuneRefused(f"for: {exc}") from None
    return out


def _tuned_rule(rule: dict[str, Any], target: dict[str, Any], c: dict[str, Any]
                ) -> dict[str, Any]:
    """The rule as it would be with the change: its PromQL and `for`."""
    new = dict(rule)
    if "params" in c:
        new["params"] = {**rule["params"], **c["params"]}
        new["expr"] = aa.render(rule["kind"], new["params"], target["app_name"],
                                target["environment"], aa._dialect(target))["expr"]
    if "expr" in c:
        new["expr"] = c["expr"]
    if "for_seconds" in c:
        new["for_seconds"] = c["for_seconds"]
    return new


def _words(rule: dict[str, Any]) -> str:
    if rule["kind"] == "custom":
        return rule["expr"]
    return aa.condition_words(rule["kind"], rule["params"], int(rule.get("for_seconds") or 0))


def preview_change(connection, settings: Any, target: dict[str, Any], rule: dict[str, Any],
                   change: dict[str, Any], *, now: Optional[datetime] = None,
                   prometheus: Any = None, scope: Any = None) -> dict[str, Any]:
    """Would it have fired in the last 24 h — as it is now, and with the change."""
    from . import rule_preview as rp

    c = checked(rule, change)
    tuned = _tuned_rule(rule, target, c)
    if prometheus is None and settings is not None:
        prometheus, scope = aa.datasource_for(connection, settings, target)

    def one(r: dict[str, Any]) -> dict[str, Any]:
        try:
            got = rp.preview(connection, target, r["expr"], f"{int(r.get('for_seconds') or 0)}s",
                             now=now, prometheus=prometheus, scope=scope, window=rp.WINDOW)
        except ValueError as exc:
            return {"fired": None, "word": f"Not previewed: {exc}"}
        return {"fired": got.get("fired"), "word": got.get("word"),
                "evaluated_by": got.get("evaluated_by")}

    return {"window": "24h", "now": {**one(rule), "condition": _words(rule)},
            "tuned": {**one(tuned), "condition": _words(tuned), "expr": tuned["expr"]}}


# --------------------------------------------------------------------------- #
# The manifest with only this rule changed
# --------------------------------------------------------------------------- #
def _scalar(value: Any) -> str:
    import yaml

    if isinstance(value, float) and value == int(value):
        value = int(value)
    body = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10 ** 6)
    return body.split("\n...")[0].strip()


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _set(text: str, edits: list, node, key: str, value: Any) -> None:
    """Edits that make `node[key]` = value: the value's own characters replaced when the key is
    there, else one line added under the mapping's last line, at its keys' indent."""
    from yaml.nodes import ScalarNode

    k, v = _get(node, key)
    if v is not None:
        if not isinstance(v, ScalarNode):
            raise TuneRefused(f"{key} is not a plain value in the manifest; change it by hand")
        rendered = _scalar(value)
        if v.style in ("|", ">"):
            rendered += "\n"
        edits.append((v.start_mark.index, v.end_mark.index, rendered))
        return
    if node.flow_style or not node.value:
        raise TuneRefused("the rule is written inline ({...}) in the manifest; change it by hand")
    indent = node.value[0][0].start_mark.column
    last = _last_end(node)
    line = " " * indent + f"{key}: {_scalar(value)}\n"
    if last.column == 0:                 # the value ended a line (a nested block): add here
        edits.append((last.index, last.index, line))
        return
    eol = text.find("\n", last.index)
    if eol < 0:
        edits.append((len(text), len(text), "\n" + line))
    else:
        edits.append((eol + 1, eol + 1, line))


def _expected(doc: dict[str, Any], rule: dict[str, Any], c: dict[str, Any], legacy: bool
              ) -> dict[str, Any]:
    """The decoded manifest the edit must produce: the old one with only this rule changed."""
    doc = copy.deepcopy(doc)
    if rule["kind"] == "custom":
        spec = next(r for r in doc["alerts"]["rules"]
                    if isinstance(r, dict) and str(r.get("alert")) == rule["alert"])
    elif legacy:
        spec = doc["metrics"]["alert"]
    else:
        spec = doc["alerts"][rule["kind"]]
    for k, v in (c.get("params") or {}).items():
        spec[_LEGACY_KEYS[k] if legacy else k] = v
    if "expr" in c:
        spec["expr"] = c["expr"]
    if "for_seconds" in c:
        if "for_seconds" in spec:
            spec["for_seconds"] = c["for_seconds"]
        else:
            spec["for"] = _for_text(c["for_seconds"])
    if "severity" in c:
        if rule["kind"] == "custom":
            spec.setdefault("labels", {})["severity"] = c["severity"]
        else:
            spec["severity"] = c["severity"]
    return doc


def _for_text(seconds: int) -> str:
    from .promql_lite import format_duration

    return format_duration(int(seconds) * 1000)


def tuned_text(text: str, rule: dict[str, Any], change: dict[str, Any]) -> str:
    """The manifest's text with only this rule's values changed. Checked twice: decoded, it is
    the old manifest with exactly this change; and every deploy's parser accepts it."""
    import yaml

    from .app_deploy_manifest import ManifestError, parse_manifest

    c = checked(rule, change)
    if not c:
        raise TuneRefused("Nothing to change")
    root = _compose(text)
    got = _find(root, rule)
    if not got:
        raise TuneRefused(f"{rule['alert']} is not in the manifest's alerts: block any more")
    node, legacy = got["node"], got["legacy"]
    if legacy and ("for_seconds" in c or "severity" in c):
        raise TuneRefused("this rule is written as metrics.alert; only its threshold, window "
                          "and minimum requests can be tuned there")
    edits: list[tuple[int, int, str]] = []
    for k, v in (c.get("params") or {}).items():
        _set(text, edits, node, _LEGACY_KEYS[k] if legacy else k, v)
    if "expr" in c:
        _set(text, edits, node, "expr", c["expr"])
    if "for_seconds" in c:
        k, _ = _get(node, "for_seconds")
        if k is not None:
            _set(text, edits, node, "for_seconds", c["for_seconds"])
        else:
            _set(text, edits, node, "for", _for_text(c["for_seconds"]))
    if "severity" in c:
        if rule["kind"] == "custom":
            _, labels = _get(node, "labels")
            if labels is None:
                raise TuneRefused("the rule has no labels: in the manifest; add its severity "
                                  "by hand")
            _set(text, edits, labels, "severity", c["severity"])
        else:
            _set(text, edits, node, "severity", c["severity"])
    out = text
    for start, end, rep in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:start] + rep + out[end:]
    old_doc = yaml.safe_load(text) or {}
    try:
        new_doc = yaml.safe_load(out) or {}
        parse_manifest(new_doc, source_text=out)
    except (ManifestError, yaml.YAMLError) as exc:
        raise TuneRefused(f"the change would break the manifest: {exc}") from None
    if new_doc != _expected(old_doc, rule, c, legacy):
        raise TuneRefused("BOSS could not change only this rule in the manifest; change it by "
                          "hand")
    if out == text:
        raise TuneRefused("Nothing to change: the manifest already says this")
    return out


# --------------------------------------------------------------------------- #
# Propose it as a pull request
# --------------------------------------------------------------------------- #
def _serving(connection, target_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql(
        "SELECT repository, ref, commit_sha, manifest->>'manifest_path' AS path "
        "FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
        "ORDER BY created_at DESC LIMIT 1"), {"t": target_id}).mappings().first()
    return dict(row) if row else None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "rule"


def propose(connection, settings: Any, target: dict[str, Any], rule_id: str,
            change: dict[str, Any], *, actor: str, reason: str = "", github: Any = None,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Open a pull request that changes only this rule in the app's boss.deploy.yaml. The
    rule's row is left as it is (the merged change arrives with the next deploy); the proposal
    is audited (`proposed`) with the pull request."""
    from .app_deploy_manifest import DEFAULT_MANIFEST_PATH

    now = now or aa._now()
    row = connection.execute(sql("SELECT * FROM app_alert_rules WHERE id = :i AND subject_id = :t"),
                             {"i": rule_id, "t": target["id"]}).mappings().first()
    if not row:
        raise LookupError("No such rule for this app")
    rule = aa._rule_dict(row)
    if via(rule) != "pull_request":
        raise TuneRefused("This rule is not declared in the repo; save it in the console")
    serving = _serving(connection, target["id"])
    if not serving or not serving.get("repository"):
        raise TuneRefused("BOSS does not know which repository this app ships from")
    repository, base = str(serving["repository"]), str(serving.get("ref") or "")
    path = str(serving.get("path") or DEFAULT_MANIFEST_PATH)
    if github is None:
        from .monitoring_pack import _github_for

        github = _github_for(connection, settings, target["project_id"], repository)
    if github is None:
        raise TuneRefused(f"BOSS cannot reach {repository} to propose the change")
    try:
        if not base:
            base, _ = github.default_branch_sha()
        text = github.read_text_file(base, path)
    except Exception as exc:  # noqa: BLE001
        raise TuneRefused(f"BOSS could not read {path} from {repository} "
                          f"({type(exc).__name__})") from None
    if text is None:
        raise TuneRefused(f"{path} is not on {base} in {repository}")
    new_text = tuned_text(text, rule, change)
    at = locate(text, rule) or {}
    c = checked(rule, change)
    tuned = _tuned_rule(rule, target, c)
    preview = None
    try:
        preview = preview_change(connection, settings, target, rule, change, now=now)
    except Exception:  # noqa: BLE001 — a preview never stops the proposal
        preview = None
    branch = f"{BRANCH_PREFIX}{_slug(rule['alert'])}-{now.strftime('%Y%m%d%H%M%S')}"
    try:
        github.ensure_branch(branch, base)
        commit = github.upsert_file(branch, path, new_text,
                                    f"alerts: tune {rule['alert']} in {path}")
    except Exception as exc:  # noqa: BLE001 — said in words, never a 500
        raise TuneRefused(f"{repository} did not take the change ({type(exc).__name__})"
                          ) from None
    before_words, after_words = _words(rule), _words(tuned)
    if "severity" in c:
        after_words += f" ({c['severity'].upper()})"
    body = "\n".join(filter(None, [
        f"Tunes the alert rule **{rule['alert']}** of {target['app_name']} "
        f"({target['environment']}), declared in `{path}` at line {at.get('line', '?')}.",
        "",
        f"- Before: {before_words}",
        f"- After: {after_words}",
        (f"- Last 24 h: {preview['now']['word']} as it is; {preview['tuned']['word']} "
         "with this change." if preview else None),
        f"- Why: {reason.strip()}" if reason.strip() else None,
        f"- Proposed by {actor} from the BOSS console. Only this rule's lines change; the "
        "rule takes effect when this merges and the app deploys.",
    ]))
    try:
        pr = github.create_pull_request(branch, f"Tune {rule['alert']}: {after_words}"[:120],
                                        body, base=base)
    except Exception as exc:  # noqa: BLE001
        raise TuneRefused(f"{repository} did not open the pull request "
                          f"({type(exc).__name__}); the change is on {branch}") from None
    url, number = getattr(pr, "url", None), getattr(pr, "number", None)
    aa._audit(connection, rule, "proposed", aa._snapshot(rule),
              {"pull_request": {"url": url, "number": number, "branch": branch,
                                "commit_sha": commit},
               "path": path, "line": at.get("line"), "change": c},
              actor, reason, now)
    return {"via": "pull_request", "pull_request": {"url": url, "number": number},
            "branch": branch, "base": base, "repository": repository, "path": path,
            "line": at.get("line"), "end_line": at.get("end_line"),
            "before": before_words, "after": after_words, "preview": preview,
            "rule_unchanged": True}


__all__ = ["TuneRefused", "checked", "locate", "preview_change", "propose", "tuned_text",
           "via", "where"]
