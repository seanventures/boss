"""Facts a model provider has actually recorded, shown before a prompt runs.

A run does not start until region, log retention, and whether the provider may
train have been shown for that prompt. A fact nobody recorded is the word
"unknown". This module does not keep a catalog, and it does not guess.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .core import make_id, stable_dedupe_key

UNKNOWN = "unknown"
FACT_KEYS = ("region", "log_retention", "may_train")
_MISSING = object()
_YES = frozenset({"yes", "true"})
_NO = frozenset({"no", "false"})


class ProviderTermsError(Exception):
    """A provider-terms request that cannot be done as asked."""


class TermsNotShown(ProviderTermsError):
    """The three facts are not on screen, so the prompt must not run."""

    def __init__(self) -> None:
        super().__init__(
            "Region, log retention, and whether the provider may train are not shown yet."
        )


class DisclosureMissing(ProviderTermsError):
    """No disclosure matches that id on this project."""

    def __init__(self) -> None:
        super().__init__("No disclosure with that id.")


def _project(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 200:
        raise ValueError("Name the project.")
    return text


def _provider(value: Any) -> tuple[str, str]:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 80:
        raise ValueError("Name the provider.")
    return text, text.casefold()


def _prompt(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 8000:
        raise ValueError("Write the prompt.")
    return text


def _text_fact(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("That fact must be words.")
    text = " ".join(value.split())
    if not text or text.lower() == UNKNOWN:
        return None
    if len(text) > 160:
        raise ValueError("That fact is too long.")
    return text


def _train_fact(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, str):
        raise ValueError("may_train must be yes or no.")
    text = " ".join(value.split()).lower()
    if not text or text == UNKNOWN:
        return None
    if text in _YES:
        return "yes"
    if text in _NO:
        return "no"
    raise ValueError("may_train must be yes or no.")


class ProviderTerms:
    """Recorded facts, the disclosure that shows them, and the one run they allow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], dict[str, str]] = {}
        self._disclosures: dict[str, dict[str, Any]] = {}
        self._by_dedupe: dict[str, str] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()
            self._disclosures.clear()
            self._by_dedupe.clear()
            self._runs.clear()

    def record(
        self,
        project_id: str,
        provider: str,
        *,
        region: Any = _MISSING,
        log_retention: Any = _MISSING,
        may_train: Any = _MISSING,
    ) -> dict[str, str]:
        """Store only the facts the caller passed. An omitted fact is left alone."""
        project = _project(project_id)
        display, key = _provider(provider)
        updates: dict[str, Optional[str]] = {}
        if region is not _MISSING:
            updates["region"] = _text_fact(region)
        if log_retention is not _MISSING:
            updates["log_retention"] = _text_fact(log_retention)
        if may_train is not _MISSING:
            updates["may_train"] = _train_fact(may_train)
        with self._lock:
            row = self._rows.setdefault((project, key), {})
            row["provider"] = display
            for name, value in updates.items():
                if value is None:
                    row.pop(name, None)
                else:
                    row[name] = value
            return self._facts_locked(project, key)

    def facts(self, project_id: str, provider: str) -> dict[str, str]:
        project = _project(project_id)
        _display, key = _provider(provider)
        with self._lock:
            return self._facts_locked(project, key)

    def disclose(self, project_id: str, provider: str, prompt: str) -> dict[str, Any]:
        """Snapshot the three facts for this prompt. Does not start a run."""
        project = _project(project_id)
        display, key = _provider(provider)
        text = _prompt(prompt)
        with self._lock:
            facts = self._facts_locked(project, key)
            stored = self._rows.get((project, key))
            if stored and stored.get("provider"):
                display = stored["provider"]
            dedupe = stable_dedupe_key(
                "provider_terms_disclosure",
                project,
                key,
                text,
                facts["region"],
                facts["log_retention"],
                facts["may_train"],
            )
            existing_id = self._by_dedupe.get(dedupe)
            if existing_id and existing_id in self._disclosures:
                return self._public(self._disclosures[existing_id])
            disclosure_id = make_id("ptd")
            row = {
                "disclosure_id": disclosure_id,
                "project_id": project,
                "provider": display,
                "provider_key": key,
                "prompt": text,
                "region": facts["region"],
                "log_retention": facts["log_retention"],
                "may_train": facts["may_train"],
                "shown": False,
                "dedupe": dedupe,
            }
            self._disclosures[disclosure_id] = row
            self._by_dedupe[dedupe] = disclosure_id
            return self._public(row)

    def mark_shown(self, project_id: str, disclosure_id: str) -> dict[str, Any]:
        """The three facts were put on screen. A run may start after this."""
        project = _project(project_id)
        with self._lock:
            row = self._owned(project, disclosure_id)
            self._require_facts(row)
            row["shown"] = True
            return self._public(row)

    def start_run(self, project_id: str, disclosure_id: str) -> dict[str, Any]:
        """Start the prompt once, and only after the three facts were shown."""
        project = _project(project_id)
        with self._lock:
            row = self._owned(project, disclosure_id)
            if not row["shown"]:
                raise TermsNotShown()
            self._require_facts(row)
            existing = self._runs.get(row["disclosure_id"])
            if existing is not None:
                repeat = dict(existing)
                repeat["deduped"] = True
                return repeat
            run = {
                "run_id": make_id("ptr"),
                "disclosure_id": row["disclosure_id"],
                "project_id": project,
                "provider": row["provider"],
                "prompt": row["prompt"],
                "region": row["region"],
                "log_retention": row["log_retention"],
                "may_train": row["may_train"],
                "started": True,
                "deduped": False,
                "vendor_called": False,
            }
            self._runs[row["disclosure_id"]] = run
            return dict(run)

    def _facts_locked(self, project: str, key: str) -> dict[str, str]:
        row = self._rows.get((project, key), {})
        return {
            "region": row.get("region") or UNKNOWN,
            "log_retention": row.get("log_retention") or UNKNOWN,
            "may_train": row.get("may_train") or UNKNOWN,
        }

    def project_of(self, disclosure_id: str) -> Optional[str]:
        """The project a disclosure was shown in (None: no such disclosure). The central
        confinement (identity.object_kinds) asks this before a handler reads the row."""
        with self._lock:
            row = self._disclosures.get(str(disclosure_id or "").strip())
            return row["project_id"] if row else None

    def _owned(self, project: str, disclosure_id: str) -> dict[str, Any]:
        row = self._disclosures.get(str(disclosure_id or "").strip())
        if row is None or row["project_id"] != project:
            raise DisclosureMissing()
        return row

    def _require_facts(self, row: dict[str, Any]) -> None:
        for key in FACT_KEYS:
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise TermsNotShown()

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "disclosure_id": row["disclosure_id"],
            "project_id": row["project_id"],
            "provider": row["provider"],
            "prompt": row["prompt"],
            "region": row["region"],
            "log_retention": row["log_retention"],
            "may_train": row["may_train"],
            "shown": bool(row["shown"]),
        }


_TERMS = ProviderTerms()


def get_terms() -> ProviderTerms:
    return _TERMS


def reset_provider_terms() -> None:
    _TERMS.reset()
