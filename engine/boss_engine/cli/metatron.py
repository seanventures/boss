"""Optional engine entry for the Metatron captain TUI.

Preferred: `make metatron` or `python3 scripts/metatron_tui.py`.
Also: `PYTHONPATH=.:engine python -m boss_engine.cli.metatron`
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_tui():
    candidates = [
        Path(__file__).resolve().parents[3] / "scripts" / "metatron_tui.py",
        Path.cwd() / "scripts" / "metatron_tui.py",
    ]
    for script in candidates:
        if script.is_file():
            spec = importlib.util.spec_from_file_location("metatron_tui", script)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit(
        "scripts/metatron_tui.py not found. Run from a BOSS checkout or use "
        "`python3 scripts/metatron_tui.py`."
    )


def main(argv: list[str] | None = None) -> int:
    return int(_load_tui().main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
