"""Phase 2 precision set: 25 finished-run records carrying 100 labelled facts (spec §6, Phase 2).

Each record is prose a real model can distil. Its gold facts are labelled `correct`. The scripted
Archivist model (`ScriptedArchivist`) replays what an extraction model returns for each record:
the gold facts, plus the noise a real model produces. The pipeline has to cope with that noise:

| record % 5 | noise the scripted model adds | what the pipeline must do |
|---|---|---|
| 0 | a plausible but wrong fact (hallucination) | nothing can catch it; it costs precision |
| 1 | a fact of an unknown kind, and one with an empty statement | drop them (validation) |
| 2 | a paraphrase of an earlier record's fact | merge it (model judgement: same) |
| 3 | a correct fact that quotes a secret | store it with the secret scrubbed |
| 4 | an update to an earlier fact (the old one is now stale) | supersede the old one |

The same records and labels are used by the real-model run (not approved yet), where the facts
come from the model instead of this script.
"""
from __future__ import annotations

import json
import re

COMPONENTS = [
    ("checkout.totals.size_band", "checkout", "orders.created", "billing"),
    ("payments.capture.capture_payment", "payments", "payments.captured", "ledger"),
    ("inventory.reserve.reserve_stock", "inventory", "stock.reserved", "fulfilment"),
    ("cart.expiry.expire_carts", "cart", "cart.expired", "marketing"),
    ("auth.session.rotate_token", "identity", "session.rotated", "audit"),
    ("search.index.reindex_product", "search", "product.indexed", "storefront"),
    ("pricing.rules.apply_discount", "pricing", "discount.applied", "analytics"),
    ("shipping.rates.quote_rate", "shipping", "rate.quoted", "checkout"),
    ("refunds.flow.issue_refund", "payments", "refund.issued", "ledger"),
    ("tax.engine.compute_tax", "tax", "tax.computed", "billing"),
    ("orders.export.export_csv", "orders", "export.ready", "support"),
    ("users.gdpr.erase_user", "identity", "user.erased", "audit"),
    ("webhooks.dispatch.deliver", "integrations", "webhook.sent", "partners"),
    ("catalog.import.load_feed", "catalog", "feed.loaded", "search"),
    ("notifications.email.send_receipt", "comms", "receipt.sent", "support"),
    ("loyalty.points.accrue_points", "loyalty", "points.accrued", "marketing"),
    ("fraud.score.score_order", "risk", "order.scored", "payments"),
    ("returns.portal.open_return", "returns", "return.opened", "fulfilment"),
    ("subscriptions.renew.renew", "billing", "subscription.renewed", "ledger"),
    ("reports.daily.build_report", "analytics", "report.built", "finance"),
    ("media.thumbs.make_thumbnail", "media", "thumbnail.made", "storefront"),
    ("gift.cards.redeem_card", "payments", "card.redeemed", "ledger"),
    ("warehouse.pick.pick_list", "fulfilment", "pick.listed", "warehouse"),
    ("i18n.locale.resolve_locale", "storefront", "locale.resolved", "search"),
    ("admin.audit.record_action", "security", "action.recorded", "audit"),
]
KIND_CYCLE = [("invariant", "lesson"), ("decision", "pattern"), ("coupling", "risk"), ("ownership", "invariant")]
SECRET = "sk_live_9fQ2xLmP4sRtV7wYzA1bC3dE"


def _gold(i: int) -> list[dict]:
    sym, team, queue, consumer = COMPONENTS[i]
    short = sym.rsplit(".", 1)[-1]
    k1, k2 = KIND_CYCLE[i % 4]
    facts = [
        {"subject": short, "predicate": "must be", "object": "idempotent per request id", "kind": "invariant",
         "statement": f"{short}() must be idempotent per request id."},
        {"subject": short, "predicate": "publishes", "object": queue, "kind": "coupling",
         "statement": f"{short}() publishes {queue}, which the {consumer} service consumes."},
        {"subject": short, "predicate": "owned by", "object": f"{team} team", "kind": "ownership",
         "statement": f"The {team} team owns {short}()."},
        {"subject": short, "predicate": "needs", "object": "a migration review", "kind": k2 if k2 != "invariant" else "risk",
         "statement": f"Changing {short}() without a migration review broke {consumer} twice."},
    ]
    if k1 == "decision":
        facts[2] = {"subject": short, "predicate": "requires", "object": "security sign-off", "kind": "decision",
                    "statement": f"Changes to {short}() require security sign-off, decided by the {team} lead."}
    return facts


def records() -> list[dict]:
    """[{id, text, gold:[fact+case], noise:[fact+case]}], cases labelled correct / wrong / stale."""
    out = []
    for i in range(len(COMPONENTS)):
        gold = [{**f, "case": f"g{i}.{j}", "label": "correct", "confidence": 0.85} for j, f in enumerate(_gold(i))]
        noise: list[dict] = []
        short = gold[0]["subject"]
        if i % 5 == 0:
            noise.append({"subject": short, "predicate": "runs", "object": "nightly", "kind": "pattern",
                          "statement": f"{short}() runs as a nightly batch job.", "case": f"h{i}", "label": "wrong",
                          "confidence": 0.6})
        elif i % 5 == 1:
            noise += [{"subject": short, "predicate": "is", "object": "fun", "kind": "trivia", "statement": "x",
                       "case": f"k{i}", "label": "wrong"},
                      {"subject": short, "predicate": "is", "object": "", "kind": "lesson", "statement": " ",
                       "case": f"e{i}", "label": "wrong"}]
        elif i % 5 == 2:
            prev = _gold(i - 1)[2]
            noise.append({**prev, "statement": prev["statement"].replace("owns", "is the owner of"),
                          "case": f"p{i}", "label": "correct", "same_as": f"g{i - 1}.2", "confidence": 0.8})
        elif i % 5 == 3:
            noise.append({"subject": short, "predicate": "reads", "object": "the payment key", "kind": "risk",
                          "statement": f"{short}() read the live payment key {SECRET} from a plain env var.",
                          "case": f"s{i}", "label": "correct", "confidence": 0.7})
        elif i % 5 == 4:
            prev = _gold(i - 1)[0]
            gold.append({**prev, "statement": prev["statement"].replace("per request id", "per order id"),
                         "case": f"u{i}", "label": "correct", "replaces": f"g{i - 1}.0", "confidence": 0.9})
        out.append({"id": f"prec-run-{i:02d}", "gold": gold, "noise": noise,
                    "text": " ".join(f["statement"] for f in gold if f["label"] == "correct")})
    return out


def labels() -> dict[str, str]:
    lab = {}
    for r in records():
        for f in r["gold"] + r["noise"]:
            lab[f["case"]] = f["label"]
    # A fact later replaced is stale: correct when written, wrong if it is still current at the end.
    for r in records():
        for f in r["gold"]:
            if f.get("replaces"):
                lab[f["replaces"]] = "stale"
    return lab


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower()).rstrip(".")


class ScriptedArchivist:
    """The scripted model: replays each record's facts and judges by the corpus's own relations."""

    def __init__(self):
        self.by_record = {r["id"]: r for r in records()}
        self.case_of = {}
        for r in records():
            for f in r["gold"] + r["noise"]:
                self.case_of[norm(f["statement"])] = f
        self.prompts: list[tuple[str, str]] = []
        self.replies: list[str] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.prompts.append((system_prompt or "", prompt))
        if (system_prompt or "").startswith("You compare"):
            ask = json.loads(prompt)
            new = self.case_of.get(norm(ask["new"]["statement"]), {})
            verdicts = {}
            for e in ask["existing"]:
                old = self.case_of.get(norm(e["statement"]), {})
                if new.get("same_as") and new["same_as"] == old.get("case"):
                    verdicts[e["id"]] = "same"
                elif new.get("replaces") and new["replaces"] == old.get("case"):
                    verdicts[e["id"]] = "contradicts"
                else:
                    verdicts[e["id"]] = "unrelated"
            reply = json.dumps(verdicts)
        else:
            rid = re.search(r"prec-run-\d\d", prompt).group(0)
            r = self.by_record[rid]
            facts = [{k: v for k, v in f.items() if k not in ("label", "same_as", "replaces")}
                     for f in r["gold"] + r["noise"]]
            for f in facts:
                f["evidence"] = [f"case:{f.pop('case')}"]
            reply = json.dumps({"facts": facts})
        self.replies.append(reply)
        return "r", reply, {"model": "scripted-archivist", "cost_usd_micros": 0,
                            "prompt_tokens": (len(system_prompt or "") + len(prompt)) // 4,
                            "completion_tokens": len(reply) // 4}
