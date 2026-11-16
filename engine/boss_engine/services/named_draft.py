"""A named draft of a published app (BOSSAPP-1245).

The draft has its own chat and its own preview. Opening one does not write a
commit and does not change the published URL. Git sync stays off.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

_SLUG = re.compile(r"[^a-z0-9]+")
_DRAFT_HOST = "https://draft.boss.test"


class NamedDraftError(Exception):
    def __init__(self, message: str, code: str = "draft_refused") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Draft:
    name: str
    slug: str
    chat_id: str
    preview_url: str
    messages: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DraftView:
    name: str
    chat_id: str
    preview_url: str
    messages: tuple[tuple[str, str], ...]
    published_url: str
    commits: tuple[str, ...]
    git_sync: bool = False
    commit_created: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "chat_id": self.chat_id,
            "preview_url": self.preview_url,
            "git_sync": self.git_sync,
            "commit_created": self.commit_created,
            "messages": [{"role": role, "text": text} for role, text in self.messages],
            "published_url": self.published_url,
            "commits": list(self.commits),
        }


class AppPress:
    """The published app. A draft may read it and must not write it."""

    def __init__(self, app_id: str, published_url: str,
                 commits: Sequence[str] = ()) -> None:
        key = str(app_id).strip()
        if not key:
            raise NamedDraftError("Name the app before opening a draft.", "app_required")
        self.app_id = key
        self.published_url = str(published_url or "").strip()
        self._commits = tuple(str(item) for item in commits)
        self._drafts: dict[str, _Draft] = {}

    @property
    def commits(self) -> tuple[str, ...]:
        return self._commits


_presses: dict[str, AppPress] = {}


def clear_named_drafts() -> None:
    _presses.clear()


def press_for(app_id: str, published_url: str,
              commits: Optional[Sequence[str]] = None) -> AppPress:
    """First registration freezes the published URL and the commit list.

    A later open that sends another URL or another commit is ignored. Creating
    a draft is not a publish and not a git sync.
    """
    key = str(app_id).strip()
    if not key:
        raise NamedDraftError("Name the app before opening a draft.", "app_required")
    found = _presses.get(key)
    if found is None:
        found = AppPress(key, published_url, commits or ())
        _presses[key] = found
    return found


def peek_press(app_id: str) -> Optional[AppPress]:
    return _presses.get(str(app_id).strip())


def _slug(name: str) -> str:
    cleaned = str(name or "").strip()
    slug = _SLUG.sub("-", cleaned.lower()).strip("-")
    slug = slug[:80].strip("-")
    if not slug:
        raise NamedDraftError("Name the draft before opening it.", "draft_name_required")
    return slug


def _preview(app_id: str, slug: str, published_url: str) -> str:
    url = f"{_DRAFT_HOST}/{app_id}/{slug}"
    if url == published_url.strip().rstrip("/"):
        return f"{url}/proof"
    return url


def _view(press: AppPress, draft: _Draft) -> DraftView:
    return DraftView(
        name=draft.name,
        chat_id=draft.chat_id,
        preview_url=draft.preview_url,
        messages=tuple(draft.messages),
        published_url=press.published_url,
        commits=press.commits,
        git_sync=False,
        commit_created=False,
    )


def open_named_draft(press: AppPress, name: str) -> DraftView:
    """Open `name` on `press`. Does not assign commits or the published URL."""
    slug = _slug(name)
    draft = press._drafts.get(slug)
    if draft is None:
        draft = _Draft(
            name=str(name).strip(),
            slug=slug,
            chat_id=f"chat_{press.app_id}_{slug}",
            preview_url=_preview(press.app_id, slug, press.published_url),
        )
        press._drafts[slug] = draft
    return _view(press, draft)


def post_draft_chat(press: AppPress, name: str, text: str) -> DraftView:
    """Append one line to that draft's chat. Still no commit and no publish."""
    slug = _slug(name)
    draft = press._drafts.get(slug)
    if draft is None:
        raise NamedDraftError("Open the draft before chatting.", "draft_not_open")
    line = str(text or "").strip()
    if not line:
        raise NamedDraftError("Write a line before sending it.", "draft_line_required")
    draft.messages.append(("operator", line))
    return _view(press, draft)
