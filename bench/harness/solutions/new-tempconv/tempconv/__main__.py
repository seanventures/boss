"""python3 -m tempconv 100C  ->  212.0F"""
from __future__ import annotations

import sys

from . import convert


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -m tempconv <value><C|F>", file=sys.stderr)
        return 2
    try:
        print(convert(argv[0]))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
