"""Canonical customer failure record validation and deterministic replay (BOSSAPP-1434)."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import yaml

from . import app_root, model_failures, private_infra, redaction, self_fix_privacy, stack_deploy

SCHEMA_VERSION = "boss.customer_failure_record/v1"
ALLOWED_SOURCES = frozenset({"share_bundle", "incident_note", "retrospective", "dogfood_run"})
SUPPORTED_KINDS = frozenset({"model_failure_explain", "app_root_java_build", "stack_deploy_explain"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUIRED_TOP_LEVEL = (
    "schema",
    "id",
    "recorded_at",
    "source",
    "title",
    "scrubbed",
    "symptom",
    "root_cause",
    "raw_evidence",
    "replay",
)


def _iter_strings(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield from _iter_strings(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def validate_record(data: Mapping[str, Any]) -> list[str]:
    """Validate schema shape and scrubbing compliance for a `boss.customer_failure_record/v1` record.
    Returns a list of human-readable error strings (empty when valid)."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["record must be a mapping"]

    for key in _REQUIRED_TOP_LEVEL:
        if key not in data or data[key] is None:
            errors.append(f"missing required field: {key}")
    if errors:
        return errors

    if data.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema must be {SCHEMA_VERSION!r}, got {data.get('schema')!r}")
    rec_id = str(data.get("id") or "")
    if not _ID_RE.fullmatch(rec_id):
        errors.append(f"id {rec_id!r} must match {_ID_RE.pattern}")
    recorded_at = str(data.get("recorded_at") or "")
    if not _DATE_RE.fullmatch(recorded_at):
        errors.append(f"recorded_at {recorded_at!r} must be YYYY-MM-DD")
    source = str(data.get("source") or "")
    if source not in ALLOWED_SOURCES:
        errors.append(f"source {source!r} must be one of {sorted(ALLOWED_SOURCES)}")
    if data.get("scrubbed") is not True:
        errors.append("scrubbed must be boolean true")
    for text_key in ("title", "symptom", "root_cause", "raw_evidence"):
        val = data.get(text_key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{text_key} must be a non-empty string")

    replay = data.get("replay")
    if not isinstance(replay, Mapping):
        errors.append("replay must be a mapping")
    else:
        kind = str(replay.get("kind") or "")
        if kind not in SUPPORTED_KINDS:
            errors.append(f"replay.kind {kind!r} must be one of {sorted(SUPPORTED_KINDS)}")
        target = replay.get("target")
        if not isinstance(target, str) or not target.strip():
            errors.append("replay.target must be a non-empty string")
        if not isinstance(replay.get("inputs"), Mapping):
            errors.append("replay.inputs must be a mapping")
        if not isinstance(replay.get("expected"), Mapping):
            errors.append("replay.expected must be a mapping")

    # Scrubbing checks across all string fields.
    for path, text in _iter_strings(data):
        if redaction.redact_secrets(text) != text:
            errors.append(f"unredacted secret detected at {path}")
        infra_hits = private_infra.findings(text)
        if infra_hits:
            errors.append(f"private infrastructure identifier at {path}: {', '.join(infra_hits)}")
        host_hits = self_fix_privacy.findings(text)
        if host_hits:
            errors.append(f"unmasked host or IP at {path}: {', '.join(host_hits)}")

    return errors


def load_record(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    return raw


def discover_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(Path(root).glob("*/record.yaml")):
        records.append((path, load_record(path)))
    return records


def assert_expected(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key, val in (expected.get("equals") or {}).items():
        assert actual.get(key) == val, f"{key}: expected {val!r}, got {actual.get(key)!r}"
    for key, val in (expected.get("not_equals") or {}).items():
        assert actual.get(key) != val, f"{key}: expected != {val!r}, got {actual.get(key)!r}"
    for key, substr in (expected.get("contains") or {}).items():
        got = str(actual.get(key) or "")
        assert str(substr) in got, f"{key}: expected {substr!r} in {got!r}"
    for key, substr in (expected.get("not_contains") or {}).items():
        got = str(actual.get(key) or "")
        assert str(substr) not in got, f"{key}: expected {substr!r} NOT in {got!r}"


def replay_record(record: Mapping[str, Any], *, work_dir: Optional[Path] = None) -> dict[str, Any]:
    """Validate and deterministically replay one customer failure record without network/LLM/Docker."""
    errors = validate_record(record)
    if errors:
        raise ValueError("invalid customer failure record: " + "; ".join(errors))

    replay = record["replay"]
    kind = str(replay["kind"])
    inputs = dict(replay.get("inputs") or {})
    expected = dict(replay.get("expected") or {})

    if kind == "model_failure_explain":
        error = inputs.get("error", "")
        explained = model_failures.explain(error)
        actual = {
            "is_storage_failure": model_failures.is_storage_failure(error),
            **explained,
        }
    elif kind == "app_root_java_build":
        files = dict(inputs.get("files") or {})
        changed = list(inputs.get("changed") or [])
        lineage = list(inputs.get("lineage") or [])
        if work_dir is not None:
            base = Path(work_dir)
            base.mkdir(parents=True, exist_ok=True)
            for rel, content in files.items():
                dest = (base / rel).resolve()
                dest.relative_to(base.resolve())
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8")
            where, tool = app_root.java_build(base, changed=changed, lineage=lineage)
        else:
            with tempfile.TemporaryDirectory(prefix="boss-replay-") as tmp:
                base = Path(tmp)
                for rel, content in files.items():
                    dest = (base / rel).resolve()
                    dest.relative_to(base.resolve())
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(str(content), encoding="utf-8")
                where, tool = app_root.java_build(base, changed=changed, lineage=lineage)
        actual = {
            "app_dir": str(where) if where is not None else None,
            "app_dir_name": where.name if where is not None else None,
            "build_tool": tool,
        }
    elif kind == "stack_deploy_explain":
        actual = stack_deploy.explain_failure(
            str(inputs.get("step") or ""),
            int(inputs.get("returncode") or 1),
            str(inputs.get("output") or ""),
            host=str(inputs.get("host") or "the host"),
            url=str(inputs.get("url") or ""),
            timeout=int(inputs.get("timeout") or 0),
        )
    else:
        raise ValueError(f"unsupported replay kind: {kind!r}")

    assert_expected(actual, expected)
    return actual
