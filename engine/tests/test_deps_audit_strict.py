"""Smoke coverage for the local fail-on-high runtime Python audit gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "deps_audit_strict.py"


def _load():
    spec = importlib.util.spec_from_file_location("deps_audit_strict", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deps_audit_strict_script_exists_and_excludes_dev_only():
    assert MODULE_PATH.is_file()
    audit = _load()
    names = {pin.name for pin in audit.load_runtime_pins(ROOT)}
    assert "pyjwt" in names
    assert "cryptography" in names
    assert "pytest" not in names
    assert "playwright" not in names


def test_deps_audit_strict_fails_on_high_not_moderate(tmp_path):
    audit = _load()
    high = tmp_path / "high.json"
    high.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "vulns": [{"id": "GHSA-high", "severity": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    moderate = tmp_path / "mod.json"
    moderate.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "vulns": [{"id": "GHSA-mod", "severity": "moderate"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert audit.evaluate(root=ROOT, python=sys.executable, json_in=high)[0] == 1
    assert audit.evaluate(root=ROOT, python=sys.executable, json_in=moderate)[0] == 0
