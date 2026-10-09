"""The built-in templates ship in the Community image (F2 §2.2).

`boss_engine.templates.service.builtin_dir()` looks for `templates/apps` above the engine
package; S2's compose proof had to mount `templates/` and set BOSS_TEMPLATES_DIR because the
image never copied it, so a real install offered no built-in template. This lays out /app the way
Dockerfile.community's COPY lines do, and loads the hello built-in from there in a fresh
interpreter with no BOSS_TEMPLATES_DIR.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "Dockerfile.community"


def _runtime_copies() -> dict[str, str]:
    """src -> dest of the plain COPY lines of the python-runtime stage (WORKDIR /app)."""
    text = DOCKERFILE.read_text()
    stage = text[text.index("AS python-runtime"):]
    stage = stage[:stage.index("\nFROM ")] if "\nFROM " in stage else stage
    assert "WORKDIR /app" in stage
    return {m.group(1): m.group(2) for m in re.finditer(r"(?m)^COPY (?!--)(\S+) (\S+)$", stage)}


def test_the_image_copies_the_templates_beside_the_engine():
    copies = _runtime_copies()
    assert copies.get("templates") == "./templates"
    assert copies.get("engine/boss_engine") == "./engine/boss_engine"


def test_hello_loads_from_the_image_path(tmp_path):
    app = tmp_path / "app"
    for src, dest in _runtime_copies().items():
        if src in ("engine/boss_engine", "templates"):
            shutil.copytree(ROOT / src, app / dest, ignore=shutil.ignore_patterns("__pycache__"))
    probe = ("import json; from boss_engine.templates import service as s; "
             "print(json.dumps({'dir': str(s.builtin_dir()), 'ids': sorted(s.builtin_ids()), "
             "'ok': {n: r.ok for n, r, _ in s.builtins()}}))")
    env = {k: v for k, v in os.environ.items() if k != "BOSS_TEMPLATES_DIR"}
    env["PYTHONPATH"] = str(app / "engine")
    out = subprocess.run([sys.executable, "-c", probe], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert Path(got["dir"]).resolve() == (app / "templates" / "apps").resolve()
    assert "hello" in got["ids"]
    assert got["ok"].get("hello") is True
