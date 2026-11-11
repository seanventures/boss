"""tempconv: convert temperatures between Celsius and Fahrenheit."""
from __future__ import annotations

import re

__all__ = ["c_to_f", "f_to_c", "convert"]

_READING = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([CcFf])\s*$")


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32) * 5 / 9


def convert(reading: str) -> str:
    """'100C' -> '212.0F', '32F' -> '0.0C'. Raises ValueError on anything else."""
    m = _READING.match(reading or "")
    if not m:
        raise ValueError(f"not a temperature: {reading!r} (expected e.g. 100C or 32F)")
    value, unit = float(m.group(1)), m.group(2).upper()
    if unit == "C":
        return f"{c_to_f(value):.1f}F"
    return f"{f_to_c(value):.1f}C"
