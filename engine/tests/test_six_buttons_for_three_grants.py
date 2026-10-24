"""Six buttons for three grants.

Reported from Metatron (`/v1/metatron/session`, one agent pipeline turn): asked to let three
repositories build, the operator saw six buttons for three decisions —

    1. Allow repository: paramg/northpeak-fineract-a, allowed: yes      [allow_repository]
    2. Allow repository: paramg/northpeak-fineract-ap, allowed: yes     [allow_repository]
    3. Allow repository: paramg/northpeak-fineract-b, allowed: yes      [allow_repository]
    4. Allow repository in northpeak-bank-a: paramg/northpeak-fineract-a  [allow_repository]
    5. Allow repository in northpeak-bank-ap: paramg/northpeak-fineract-ap [allow_repository]
    6. Allow repository in northpeak-bank-b: paramg/northpeak-fineract-b  [allow_repository]

Two sources offered each grant: the tool's own held write (`grants_authority` is always a
button; its label and args come from the write gate, which fills `allowed` explicitly) and the
model's own `propose_action` for the identical call, phrased with the project name and — because
`allowed` defaults to `True` — written without that argument at all.

`_same_offer` compared literal args, so `{"repository": r, "allowed": True}` (the held write)
never equalled `{"repository": r}` (the model's own call) even though they are the same grant.
Canonicalizing args against the tool's schema — filling an omitted optional argument with its
declared default before comparing — is what makes them equal regardless of which source, and
which label, got there first.
"""

from boss_engine.services.intent_router import remember_offer


class _FakeSpec:
    def __init__(self, parameters):
        self.parameters = parameters


class _FakeRegistry:
    """Just enough of `ToolRegistry` for `_canonical_args`: `.get(name).parameters`."""

    def __init__(self, schemas):
        self._schemas = schemas

    def get(self, name):
        return _FakeSpec(self._schemas[name])


ALLOW_REPOSITORY_REGISTRY = _FakeRegistry({
    "allow_repository": {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "allowed": {"type": "boolean", "default": True},
        },
        "required": ["repository"],
    },
})


def _labels(offers, registry=None):
    out = []
    for o in offers:
        remember_offer(out, o, registry=registry)
    return [x["label"] for x in out]


class TestSixButtonsForThreeGrants:
    def test_the_reported_case_collapses_to_three(self):
        held_writes = [
            {"tool": "allow_repository",
             "label": f"Allow repository: paramg/northpeak-fineract-{suffix}, allowed: yes",
             "args": {"repository": f"paramg/northpeak-fineract-{suffix}", "allowed": True}}
            for suffix in ("a", "ap", "b")
        ]
        model_proposals = [
            {"tool": "allow_repository",
             "label": f"Allow repository in northpeak-bank-{suffix}: "
                      f"paramg/northpeak-fineract-{suffix}",
             "args": {"repository": f"paramg/northpeak-fineract-{suffix}"}}
            for suffix in ("a", "ap", "b")
        ]
        labels = _labels(held_writes + model_proposals, registry=ALLOW_REPOSITORY_REGISTRY)
        assert len(labels) == 3
        # The richer, project-naming label wins over the write gate's generic fallback.
        assert labels == [
            "Allow repository in northpeak-bank-a: paramg/northpeak-fineract-a",
            "Allow repository in northpeak-bank-ap: paramg/northpeak-fineract-ap",
            "Allow repository in northpeak-bank-b: paramg/northpeak-fineract-b",
        ]

    def test_order_does_not_matter_the_richer_label_still_wins(self):
        out = []
        remember_offer(out, {
            "tool": "allow_repository",
            "label": "Allow repository in northpeak-bank-a: paramg/northpeak-fineract-a",
            "args": {"repository": "paramg/northpeak-fineract-a"},
        }, registry=ALLOW_REPOSITORY_REGISTRY)
        remember_offer(out, {
            "tool": "allow_repository",
            "label": "Allow repository: paramg/northpeak-fineract-a, allowed: yes",
            "args": {"repository": "paramg/northpeak-fineract-a", "allowed": True},
        }, registry=ALLOW_REPOSITORY_REGISTRY)
        assert len(out) == 1
        assert out[0]["label"] == (
            "Allow repository in northpeak-bank-a: paramg/northpeak-fineract-a")

    def test_without_a_schema_the_two_offers_are_kept_rather_than_guessed_equal(self):
        """No registry: there is no default to consult, so a missing `allowed` is NOT assumed
        to mean the same thing as an explicit one. Keeping both is the safe failure — a dedupe
        must never eat a genuinely different offer to look tidy."""
        out = _labels([
            {"tool": "allow_repository",
             "label": "Allow repository: paramg/northpeak-fineract-a, allowed: yes",
             "args": {"repository": "paramg/northpeak-fineract-a", "allowed": True}},
            {"tool": "allow_repository",
             "label": "Allow repository in northpeak-bank-a: paramg/northpeak-fineract-a",
             "args": {"repository": "paramg/northpeak-fineract-a"}},
        ])
        assert len(out) == 2

    def test_a_real_difference_in_the_non_defaulted_argument_still_survives(self):
        """Two different repositories must never collapse, schema or not."""
        out = _labels([
            {"tool": "allow_repository",
             "label": "Allow repository: paramg/northpeak-fineract-a, allowed: yes",
             "args": {"repository": "paramg/northpeak-fineract-a", "allowed": True}},
            {"tool": "allow_repository",
             "label": "Allow repository in northpeak-bank-ap: paramg/northpeak-fineract-ap",
             "args": {"repository": "paramg/northpeak-fineract-ap"}},
        ], registry=ALLOW_REPOSITORY_REGISTRY)
        assert len(out) == 2
