"""Deterministic fact lines. This is not a model extractor.

A fact line is one whole line: ``<token> requires|owns|calls|decided <token>``.
Paraphrases are labelled in the conformance set and are misses. The extractor
never reads that set.
"""
from __future__ import annotations

import re
from typing import Any

_LINE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*) (requires|owns|calls|decided) ([A-Za-z][A-Za-z0-9_]*)$"
)
_KIND = {
    "requires": "invariant",
    "owns": "ownership",
    "calls": "coupling",
    "decided": "decision",
}
_PREDICATES = ("requires", "owns", "calls", "decided")


class LineExtractor:
    def extract(self, text: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for raw in (text or "").splitlines():
            match = _LINE.match(raw.strip())
            if match is None:
                continue
            subject, predicate, obj = match.groups()
            facts.append(
                {
                    "kind": _KIND[predicate],
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "statement": f"{subject} {predicate} {obj}",
                    "confidence": 0.9,
                }
            )
        return facts


def conformance_corpus() -> list[dict[str, Any]]:
    """100 texts and 100 labelled facts. The labels are not passed to extract."""
    items: list[dict[str, Any]] = []
    for index in range(60):
        predicate = _PREDICATES[index % 4]
        subject = f"subject{index:02d}"
        obj = f"object{index:02d}"
        items.append(
            {
                "text": f"{subject} {predicate} {obj}",
                "facts": [(subject, predicate, obj)],
            }
        )
    for index in range(10):
        left = 60 + index
        right = 70 + index
        left_predicate = _PREDICATES[index % 4]
        right_predicate = _PREDICATES[(index + 1) % 4]
        left_subject, left_object = f"subject{left:02d}", f"object{left:02d}"
        right_subject, right_object = f"subject{right:02d}", f"object{right:02d}"
        items.append(
            {
                "text": (
                    f"{left_subject} {left_predicate} {left_object}\n"
                    f"{right_subject} {right_predicate} {right_object}"
                ),
                "facts": [
                    (left_subject, left_predicate, left_object),
                    (right_subject, right_predicate, right_object),
                ],
            }
        )
    for index in range(20):
        number = 80 + index
        subject = f"subject{number:02d}"
        obj = f"object{number:02d}"
        predicate = _PREDICATES[index % 4]
        if predicate == "requires":
            text = f"The rule for {subject} is that it needs {obj} before it can proceed."
        elif predicate == "owns":
            text = f"The owner of {subject} is {obj}."
        elif predicate == "calls":
            text = f"{subject} invokes {obj} during checkout."
        else:
            text = f"A person decided that {subject} would use {obj}."
        items.append({"text": text, "facts": [(subject, predicate, obj)]})
    for text in (
        "please review the notes before the meeting",
        "the handbook mentions reserve in passing",
        "see the diagram on page twelve",
        "no relation is stated here",
        "questions go to the channel",
        "example key AKIAIOSFODNN7EXAMPLE was rotated",
        "mail the owner at ada@example.com",
        "token sk-abcdefghijklmnopqrstuvwxyz stays out of the store",
        "Authorization: Bearer abcdefghijklmnop1234",
        "the word requires appears without a subject token line",
    ):
        items.append({"text": text, "facts": []})
    if len(items) != 100:
        raise RuntimeError("conformance corpus must contain 100 texts")
    return items


def score_extractor(extractor: LineExtractor, corpus: list[dict[str, Any]]) -> dict[str, Any]:
    true_positive = 0
    false_positive = 0
    false_negative = 0
    for item in corpus:
        got = {
            (fact["subject"], fact["predicate"], fact["object"])
            for fact in extractor.extract(item["text"])
        }
        gold = {tuple(fact) for fact in item["facts"]}
        true_positive += len(got & gold)
        false_positive += len(got - gold)
        false_negative += len(gold - got)
    labelled = true_positive + false_negative
    predicted = true_positive + false_positive
    precision = (true_positive / predicted) if predicted else 1.0
    recall = (true_positive / labelled) if labelled else 1.0
    return {
        "items": len(corpus),
        "labelled_facts": labelled,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "extractor": "line",
        "model_precision": None,
    }
