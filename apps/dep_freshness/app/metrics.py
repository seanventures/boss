"""Hand-rolled stdlib metrics registry, per the BOSS platform observability standard.

No third-party metrics client dependency (criterion 13). Renders Prometheus
text exposition format 0.0.4.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{escape_label_value(value)}"' for key, value in labels.items()]
    return "{" + ",".join(parts) + "}"


class Counter:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        labels = labels or {}
        return tuple(labels.get(name, "") for name in self.label_names)

    def inc(self, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def header_lines(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]

    def data_lines(self) -> list[str]:
        lines = []
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            labels = dict(zip(self.label_names, key))
            lines.append(f"{self.name}{_format_labels(labels)} {_format_number(value)}")
        return lines


class Histogram:
    DEFAULT_BUCKETS: tuple[float, ...] = (
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10,
    )

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.buckets = tuple(buckets)
        self._bucket_counts: dict[tuple[str, ...], list[float]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._counts: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        labels = labels or {}
        return tuple(labels.get(name, "") for name in self.label_names)

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(labels)
        with self._lock:
            if key not in self._bucket_counts:
                self._bucket_counts[key] = [0.0] * len(self.buckets)
                self._sums[key] = 0.0
                self._counts[key] = 0.0
            counts = self._bucket_counts[key]
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1.0
            self._sums[key] += value
            self._counts[key] += 1.0

    def header_lines(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]

    def data_lines(self) -> list[str]:
        lines: list[str] = []
        with self._lock:
            keys = list(self._bucket_counts.keys())
            bucket_counts = {k: list(v) for k, v in self._bucket_counts.items()}
            sums = dict(self._sums)
            counts = dict(self._counts)
        for key in keys:
            labels = dict(zip(self.label_names, key))
            cumulative = 0.0
            for bound, count in zip(self.buckets, bucket_counts[key]):
                cumulative = count
                bucket_labels = {**labels, "le": _format_le(bound)}
                lines.append(
                    f"{self.name}_bucket{_format_labels(bucket_labels)} {_format_number(cumulative)}"
                )
            inf_labels = {**labels, "le": "+Inf"}
            lines.append(
                f"{self.name}_bucket{_format_labels(inf_labels)} {_format_number(counts[key])}"
            )
            lines.append(f"{self.name}_sum{_format_labels(labels)} {_format_number(sums[key])}")
            lines.append(f"{self.name}_count{_format_labels(labels)} {_format_number(counts[key])}")
        return lines


def _format_le(bound: float) -> str:
    if float(bound).is_integer():
        return str(int(bound))
    return repr(float(bound))


class Gauge:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        labels = labels or {}
        return tuple(labels.get(name, "") for name in self.label_names)

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def header_lines(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]

    def data_lines(self) -> list[str]:
        lines = []
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            labels = dict(zip(self.label_names, key))
            lines.append(f"{self.name}{_format_labels(labels)} {_format_number(value)}")
        return lines


class MetricsRegistry:
    def __init__(self, version: str):
        self._start = time.monotonic()
        self.http_requests_total = Counter(
            "http_requests_total",
            "Total HTTP requests.",
            ("method", "route", "status"),
        )
        self.http_request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
        )
        self.app_info = Gauge("app_info", "Static application build info.", ("version",))
        self.app_info.set(1, {"version": version})
        self.app_uptime_seconds = Gauge("app_uptime_seconds", "Process uptime in seconds.")

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start

    def render(self) -> str:
        self.app_uptime_seconds.set(self.uptime_seconds())
        lines: list[str] = []
        lines.extend(self.http_requests_total.header_lines())
        lines.extend(self.http_request_duration_seconds.header_lines())
        lines.extend(self.app_info.header_lines())
        lines.extend(self.app_uptime_seconds.header_lines())
        # Data lines are grouped after all headers, with metrics that are always
        # populated (gauges) first, so that whichever counter/histogram a caller
        # just touched renders as the trailing content of the exposition text.
        lines.extend(self.app_uptime_seconds.data_lines())
        lines.extend(self.app_info.data_lines())
        lines.extend(self.http_request_duration_seconds.data_lines())
        lines.extend(self.http_requests_total.data_lines())
        return "\n".join(lines) + "\n"
