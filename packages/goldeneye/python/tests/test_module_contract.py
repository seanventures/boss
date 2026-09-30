"""Validate every modules/*/package.yaml against the module contract schema.

Mission fields: api_version, id, version, layer (plus name/kind from schema).
Does not require the jsonschema package — performs equivalent field checks so
the kit stays testable with stdlib only. If jsonschema is installed, full
Draft 2020-12 validation also runs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "python"))

from goldeneye_kit.catalog import discover_modules, load_module_manifest  # noqa: E402

SCHEMA_PATH = KIT / "schemas" / "package.schema.json"
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
KIND_ENUM = {
    "host",
    "runtime",
    "edge",
    "control",
    "data",
    "observe",
    "tool",
    "optional",
    "integration",
}


def _read_raw_yaml(path: Path) -> dict[str, Any]:
    """Minimal YAML load (PyYAML if present, else catalog helper)."""
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        from goldeneye_kit.catalog import _read_yaml  # type: ignore

        return _read_yaml(path)


def test_schema_file_exists_and_requires_core_fields():
    assert SCHEMA_PATH.is_file(), f"missing schema at {SCHEMA_PATH}"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    required = set(schema.get("required") or [])
    for field in ("api_version", "id", "version", "layer"):
        assert field in required, f"schema must require {field}"
    # Full schema also requires name + kind
    assert "name" in required
    assert "kind" in required


def test_all_module_package_yaml_fields():
    modules = discover_modules(KIT)
    assert modules, "expected at least one module under modules/"

    errors: list[str] = []
    for mod_dir in modules:
        mf = mod_dir / "package.yaml"
        if not mf.is_file():
            errors.append(f"{mod_dir.name}: missing package.yaml")
            continue
        raw = _read_raw_yaml(mf)
        mid = str(raw.get("id") or mod_dir.name)

        # Required contract fields (mission + schema)
        for field in ("api_version", "id", "version", "layer"):
            if field not in raw or raw.get(field) in (None, ""):
                errors.append(f"{mid}: missing required field {field}")

        if "name" not in raw or not str(raw.get("name") or "").strip():
            errors.append(f"{mid}: missing required field name")
        if "kind" not in raw or not str(raw.get("kind") or "").strip():
            errors.append(f"{mid}: missing required field kind")

        # Types / constraints matching package.schema.json
        api = raw.get("api_version")
        if api is not None and int(api) != 1:
            errors.append(f"{mid}: api_version must be 1, got {api!r}")

        if raw.get("id") is not None and not ID_RE.match(str(raw["id"])):
            errors.append(f"{mid}: id must match ^[a-z][a-z0-9-]*$")

        if raw.get("id") is not None and str(raw["id"]) != mod_dir.name:
            errors.append(
                f"{mid}: id {raw['id']!r} must match directory name {mod_dir.name!r}"
            )

        layer = raw.get("layer")
        if layer is not None:
            try:
                layer_i = int(layer)
            except (TypeError, ValueError):
                errors.append(f"{mid}: layer must be int, got {layer!r}")
            else:
                if layer_i < 0 or layer_i > 9:
                    errors.append(f"{mid}: layer {layer_i} out of range 0–9")

        kind = raw.get("kind")
        if kind is not None and str(kind) not in KIND_ENUM:
            errors.append(f"{mid}: kind {kind!r} not in {sorted(KIND_ENUM)}")

        # Hooks files exist when declared
        hooks = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}
        for hook_name, script in (hooks or {}).items():
            if not script:
                continue
            script_path = mod_dir / str(script)
            if not script_path.is_file():
                errors.append(
                    f"{mid}: hooks.{hook_name}={script} missing at {script_path}"
                )

        # install.sh is required by MODULE_CONTRACT unless hooks.install overrides
        install_name = str((hooks or {}).get("install") or "install.sh")
        if not (mod_dir / install_name).is_file():
            errors.append(f"{mid}: missing install hook {install_name}")

        # check.sh should exist (mission kit depth)
        check_name = str((hooks or {}).get("check") or "check.sh")
        if not (mod_dir / check_name).is_file():
            errors.append(f"{mid}: missing check hook {check_name}")

        # Assembled manifest still loads
        loaded = load_module_manifest(mod_dir)
        assert loaded["id"] == mid or loaded["id"] == mod_dir.name
        assert loaded["api_version"] == 1
        assert isinstance(loaded["layer"], int)
        assert loaded["version"]

    assert not errors, "module contract violations:\n- " + "\n- ".join(errors)


def test_jsonschema_validation_when_available():
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return  # optional dependency

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for mod_dir in discover_modules(KIT):
        mf = mod_dir / "package.yaml"
        if not mf.is_file():
            continue
        raw = _read_raw_yaml(mf)
        jsonschema.validate(instance=raw, schema=schema)
