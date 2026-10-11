"""`python -m boss_engine.platform <command>`: the plugin SDK's command line.

    schemas                  regenerate the committed boss.plugin/v1 JSON Schemas
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["schemas"]:
        from .contract import write_schemas
        for p in write_schemas():
            print(p)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
