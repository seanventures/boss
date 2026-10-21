"""The spike corpus: 200 BOSS run-record episodes and 50 hand-labelled facts (spec §3.2).

The setting is the case study's NorthPeak Retail (Saleor plus northpeak-fulfilment). Every
fact-bearing sentence in an episode states exactly one of the 50 labelled facts below; every
other sentence is deliberately NOT durable knowledge (timings, costs, chatter), so an extractor
that keeps it is wrong (the spec: atomic facts, never transcripts). That makes precision and
recall computable without a judge: precision = extracted facts that match a label / all
extracted; recall = labels recovered / 50.

Facts F41–F44 supersede earlier ones (temporal handling: the older fact's validity must close).
Episodes are generated deterministically (seeded) from the labels; the labels are hand-written.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KINDS = ("invariant", "decision", "lesson", "pattern", "coupling", "ownership", "risk")


@dataclass(frozen=True)
class Fact:
    id: str
    kind: str
    subject: str
    predicate: str
    object: str
    statement: str
    task: str
    supersedes: str | None = None


# The 50 hand-labelled facts.
FACTS: tuple[Fact, ...] = (
    Fact("F01", "coupling", "northpeak-fulfilment", "consumes", "Saleor ORDER_CREATED webhook",
         "northpeak-fulfilment consumes the Saleor ORDER_CREATED webhook.", "T1"),
    Fact("F02", "coupling", "northpeak-fulfilment", "writes", "stock_reservation table",
         "northpeak-fulfilment writes to the shared stock_reservation table.", "T1"),
    Fact("F03", "coupling", "Saleor checkout", "reads", "stock_reservation table",
         "Saleor checkout reads the stock_reservation table before completing an order.", "T1"),
    Fact("F04", "invariant", "stock reservation", "is keyed by", "order line id",
         "A stock reservation is keyed by the order line id, never by the order id.", "T1"),
    Fact("F05", "invariant", "webhook handler", "must be", "idempotent on event id",
         "The fulfilment webhook handler must be idempotent on the webhook event id.", "T1"),
    Fact("F06", "decision", "webhook signatures", "are verified with", "HMAC-SHA256 secret per app",
         "Webhook signatures are verified with a per-app HMAC-SHA256 secret; signed off by the operator.", "T1"),
    Fact("F07", "invariant", "reserved quantity", "never exceeds", "warehouse stock",
         "The reserved quantity for a variant never exceeds the warehouse stock.", "T1"),
    Fact("F08", "ownership", "northpeak-fulfilment", "is owned by", "fulfilment team",
         "The fulfilment team owns northpeak-fulfilment.", "T1"),
    Fact("F09", "decision", "click and collect", "is modelled as", "a Saleor shipping method",
         "Click and collect is modelled as a Saleor shipping method, not a new checkout flow.", "T2"),
    Fact("F10", "invariant", "pickup slot", "has capacity", "per store per hour",
         "A pickup slot has a capacity per store per hour.", "T2"),
    Fact("F11", "decision", "auth changes", "need", "security sign-off",
         "Auth changes need security sign-off; signed by the operator during T2.", "T2"),
    Fact("F12", "coupling", "click and collect", "reserves via", "stock_reservation table",
         "Click and collect reserves stock through the stock_reservation table.", "T2"),
    Fact("F13", "invariant", "checkout total", "is computed by", "the Saleor checkout calculations module",
         "The checkout total is computed only by the Saleor checkout calculations module.", "T2"),
    Fact("F14", "lesson", "frontend checks", "should run", "before backend tests",
         "Running the frontend checks before the backend tests catches most review rounds early.", "T2"),
    Fact("F15", "pattern", "Saleor discounts", "are applied in", "the discount pipeline order",
         "Saleor discounts are applied in a fixed pipeline: catalogue, then voucher, then order.", "T3"),
    Fact("F16", "invariant", "buy 3 pay 2", "applies to", "the cheapest item in the category",
         "Buy 3 pay 2 makes the cheapest qualifying item in the category free.", "T3"),
    Fact("F17", "lesson", "promotion rules", "need tests for", "mixed-category baskets",
         "Promotion rules need tests for mixed-category baskets; the T3 review found that gap.", "T3"),
    Fact("F18", "risk", "discount rounding", "can cause", "one-cent totals mismatch",
         "Discount rounding can cause a one-cent mismatch between line totals and the order total.", "T3"),
    Fact("F19", "invariant", "promotions", "must not stack with", "staff discounts",
         "Promotions must not stack with staff discounts.", "T3"),
    Fact("F20", "risk", "reservation off-by-one", "caused", "overselling",
         "An off-by-one in the reservation quantity check caused customers to be oversold.", "T4"),
    Fact("F21", "decision", "oversell incident root cause", "is", "a less-than-or-equal check in reserve_stock",
         "Root cause of the oversell incident: reserve_stock compared with <= instead of <.", "T4"),
    Fact("F22", "pattern", "oversell alerts", "fire on", "negative available stock",
         "The oversell alert fires when available stock goes negative for any variant.", "T4"),
    Fact("F23", "lesson", "reservation changes", "need", "a concurrency test",
         "Any change to reservation logic needs a concurrency test with parallel checkouts.", "T4"),
    Fact("F24", "ownership", "oversell runbook", "is owned by", "Beacon",
         "Beacon owns the oversell runbook.", "T4"),
    Fact("F25", "decision", "VAT for Norway", "uses", "the Saleor tax app",
         "VAT for Norway is handled by the Saleor tax app rather than flat rates.", "T5"),
    Fact("F26", "invariant", "taxes", "are applied after", "discounts",
         "Taxes are applied after discounts, never before.", "T5"),
    Fact("F27", "coupling", "tax app", "is called by", "checkout calculations",
         "The tax app is called by the checkout calculations module.", "T5"),
    Fact("F28", "decision", "stock_reservation migration", "uses", "expand and contract",
         "The stock_reservation migration uses expand and contract with a dual-write window.", "T6"),
    Fact("F29", "invariant", "migrations", "must keep", "the old column readable during dual write",
         "Migrations must keep the old column readable for the whole dual-write window.", "T6"),
    Fact("F30", "risk", "schema change on stock_reservation", "breaks", "northpeak-fulfilment",
         "A schema change on stock_reservation breaks northpeak-fulfilment unless both deploy together.", "T6"),
    Fact("F31", "pattern", "spend threshold promotions", "reuse", "the buy 3 pay 2 rule engine",
         "Spend-threshold promotions reuse the rule engine built for buy 3 pay 2.", "T7"),
    Fact("F32", "decision", "staff API tokens", "are scoped to", "one warehouse",
         "Staff API tokens are scoped to a single warehouse; signed off by security.", "T8"),
    Fact("F33", "invariant", "warehouse scope", "is enforced in", "the permission layer",
         "Warehouse scope is enforced in the permission layer, not in each resolver.", "T8"),
    Fact("F34", "risk", "returns restock", "can double count", "stock",
         "The returns flow can restock an item twice and double count stock.", "T9"),
    Fact("F35", "decision", "returns oversell root cause", "is", "restock called by both refund and return",
         "Root cause of the returns oversell: restock ran from both the refund and the return handler.", "T9"),
    Fact("F36", "lesson", "oversell incidents", "start with", "the reservation invariant",
         "Oversell investigations should start from the reservation invariant; T4 and T9 shared it.", "T9"),
    Fact("F37", "risk", "checkout complete", "issues", "N+1 queries on order lines",
         "The checkout complete mutation issues N+1 queries on order lines.", "T10"),
    Fact("F38", "pattern", "order line queries", "use", "prefetch_related",
         "Order line queries use prefetch_related to avoid N+1 queries.", "T10"),
    Fact("F39", "decision", "checkout complete", "is split into", "CheckoutCompleteService",
         "The checkout complete mutation is split into a CheckoutCompleteService class.", "T11"),
    Fact("F40", "invariant", "CheckoutCompleteService", "is the only writer of", "orders from checkouts",
         "CheckoutCompleteService is the only code that creates orders from checkouts.", "T11"),
    Fact("F41", "decision", "reservation TTL", "is", "10 minutes",
         "The reservation TTL is now 10 minutes.", "T6", supersedes="F45"),
    Fact("F42", "ownership", "oversell runbook", "is owned by", "fulfilment team",
         "The oversell runbook moved to the fulfilment team.", "T9", supersedes="F24"),
    Fact("F43", "decision", "webhook signatures", "are verified with", "asymmetric JWS keys",
         "Webhook signatures are now verified with asymmetric JWS keys.", "T8", supersedes="F06"),
    Fact("F44", "pattern", "oversell alerts", "fire on", "available stock below safety stock",
         "The oversell alert now fires when available stock drops below safety stock.", "T10", supersedes="F22"),
    Fact("F45", "decision", "reservation TTL", "is", "15 minutes",
         "Stock reservations expire after 15 minutes.", "T1"),
    Fact("F46", "coupling", "Saleor order webhooks", "are retried by", "the Saleor webhook scheduler",
         "Saleor retries failed order webhooks through its webhook scheduler.", "T1"),
    Fact("F47", "lesson", "Beacon alerts", "should link", "the runbook",
         "Every Beacon alert should link its runbook; T4 lost time without one.", "T4"),
    Fact("F48", "invariant", "refunds", "never", "restock automatically",
         "Refunds never restock automatically; only the return handler restocks.", "T9"),
    Fact("F49", "decision", "load tests", "run against", "staging with production-sized data",
         "Load tests run against staging with production-sized data.", "T10"),
    Fact("F50", "ownership", "CheckoutCompleteService", "is owned by", "checkout team",
         "The checkout team owns CheckoutCompleteService.", "T11"),
)

# Sentences that carry no durable knowledge. Keeping them is a precision error.
NOISE = (
    "The build took {n} minutes.", "Run {n} used {m} turns.", "Receipt: ${c} of model spend.",
    "Rook approved after {m} review round(s).", "CI job {n} was retried once after a runner restart.",
    "The panel met for {m} minutes.", "Deploy {n} finished at 14:{m:02d}.",
    "Sentry raised no advisories this time.", "The staging host 203.0.113.{m} was used.",
    "Coffee machine on floor 3 is broken again.",
)
EPISODE_KINDS = ("spec", "panel minutes", "decision", "review finding", "release card",
                 "incident", "runbook", "run summary")
TASK_ORDER = ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11")


@dataclass
class Episode:
    id: str
    kind: str
    task: str
    reference_time: datetime
    body: str
    fact_ids: list[str] = field(default_factory=list)


def _phrase(fact: Fact, rng: random.Random) -> str:
    forms = (
        fact.statement,
        f"Note for later runs: {fact.statement[0].lower()}{fact.statement[1:]}",
        f"Recorded: {fact.statement}",
        f"As agreed, {fact.statement[0].lower()}{fact.statement[1:]}",
    )
    return rng.choice(forms)


def episodes(count: int = 200, seed: int = 7) -> list[Episode]:
    """Deterministic episodes in task order; each labelled fact appears at least twice."""
    rng = random.Random(seed)
    by_task = {t: [f for f in FACTS if f.task == t] for t in TASK_ORDER}
    start = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    out: list[Episode] = []
    per_task = count // len(TASK_ORDER)
    extra = count - per_task * len(TASK_ORDER)
    seen: dict[str, int] = {f.id: 0 for f in FACTS}
    for ti, task in enumerate(TASK_ORDER):
        n = per_task + (1 if ti < extra else 0)
        pool = by_task[task]
        for j in range(n):
            # Cover every fact of the task first, then sample; 1–3 facts per episode.
            unseen = [f for f in pool if seen[f.id] < 2]
            k = min(len(pool), rng.choice((1, 2, 2, 3)))
            chosen = (unseen[:k] if unseen else rng.sample(pool, k))
            if len(chosen) < k:
                chosen += rng.sample([f for f in pool if f not in chosen], k - len(chosen))
            for f in chosen:
                seen[f.id] += 1
            sentences = [_phrase(f, rng) for f in chosen]
            for _ in range(rng.choice((1, 2, 3))):
                sentences.insert(rng.randrange(len(sentences) + 1), rng.choice(NOISE).format(
                    n=rng.randrange(1000, 9999), m=rng.randrange(1, 59), c=f"{rng.uniform(0.2, 9):.2f}"))
            kind = rng.choice(EPISODE_KINDS)
            out.append(Episode(
                id=f"E{len(out) + 1:03d}", kind=kind, task=task,
                reference_time=start + timedelta(days=ti * 3, hours=j),
                body=f"[{kind} · {task} · NorthPeak Retail] " + " ".join(sentences),
                fact_ids=[f.id for f in chosen]))
    return out


FACT_BY_ID = {f.id: f for f in FACTS}
