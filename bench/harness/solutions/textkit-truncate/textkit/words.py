"""Counting and picking words in plain text."""
from __future__ import annotations


def word_count(text: str) -> int:
    """How many words `text` holds."""
    if not text:
        return 0
    return len(text.split(" "))


def first_words(text: str, n: int) -> str:
    """The first `n` words of `text`, joined by single spaces."""
    if n < 0:
        raise ValueError("n must not be negative")
    return " ".join(text.split()[:n])


def truncate_words(text: str, limit: int, suffix: str = "…") -> str:
    """`text` cut to its first `limit` words plus `suffix`, or unchanged when it is short enough."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + suffix
