"""The AML case study's synthetic bank (case study 4): NorthPeak Lending's customers, accounts,
counterparties, 90 days of transactions, KYC profiles and three monitoring alerts.

DETERMINISTIC AND SYNTHETIC. Every value comes from `random.Random(SEED)` and fixed tables below:
no download, no real customer, no clock. `generate()` returns the same dataset on every machine,
so the mock MCP connectors (tools/mcp_aml_mock.py), the graph the sample loads
(services/knowledge/aml_sample.py) and the tests all read one bank.

Two patterns are planted, and their ground truth is kept here (`GROUND_TRUTH`) for tests only —
nothing a role agent sees says which alert is which:

  (a) PAYROLL FAN-OUT, BENIGN. Harbor Point Cafe LLC pays 14 staff every other Friday from its
      payroll account; its sister company Harbor Point Catering LLC (same owner, funded from the
      cafe's operating account) pays 9, five of them the same people. Ten-plus payments to distinct
      parties in a day looks like a funnel account. KYC documents the payroll.
      ALERT-1 is raised on the cafe's payroll account; ALERT-2 on the catering payroll account —
      the related entity, so its investigation should find what ALERT-1's determination taught.
  (b) STRUCTURING AND RAPID MOVEMENT, REAL. Quentin Marlow deposits cash just under the $10,000
      reporting threshold several times a week and wires it within a day or two to three related
      offshore companies (one registered address, one director); one of them wires money back to
      his relative Rhea Marlow's account. ALERT-3, a true positive for the escalation path.

Pure standard library: imported by the mcp-tools service and the engine alike.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

SEED = 20261101
BANK = "NorthPeak Lending"
END = date(2026, 10, 28)
DAYS = 90
START = END - timedelta(days=DAYS - 1)
CTR_THRESHOLD = 10_000.00

#: For tests and the runbook only. Never shown to an agent.
GROUND_TRUTH: dict[str, dict[str, str]] = {
    "ALERT-1": {"label": "false_positive", "pattern": "payroll_fan_out",
                "why": "documented biweekly payroll of Harbor Point Cafe LLC"},
    "ALERT-2": {"label": "false_positive", "pattern": "payroll_fan_out",
                "why": "documented payroll of Harbor Point Catering LLC, the cafe's sister company"},
    "ALERT-3": {"label": "true_positive", "pattern": "structuring_rapid_movement",
                "why": "sub-threshold cash deposits wired on to three related offshore companies, "
                       "with funds cycling back to a relative"},
}

_FIRST = ["Ava", "Liam", "Maya", "Noah", "Priya", "Omar", "Sofia", "Ethan", "Lena", "Mateo",
          "Grace", "Ravi", "Chloe", "Daniel", "Amara", "Jonah", "Iris", "Felix", "Nadia", "Tom",
          "Hana", "Marco", "Zoe", "Samir", "Elena", "Kofi", "Ruth", "Leo", "Mina", "Owen"]
_LAST = ["Alvarez", "Brennan", "Chen", "Dubois", "Eriksen", "Fofana", "Garcia", "Hughes",
         "Ibrahim", "Jensen", "Kowalski", "Lindqvist", "Moreau", "Nakamura", "Osei", "Patel",
         "Quinn", "Rossi", "Silva", "Tanaka", "Underwood", "Varga", "Walsh", "Yilmaz", "Zhou"]
_OCCUPATIONS = ["nurse", "teacher", "software developer", "electrician", "retail manager",
                "accountant", "line cook", "delivery driver", "pharmacist", "graphic designer",
                "warehouse supervisor", "dental hygienist", "civil engineer", "barista"]

#: Employers that pay ordinary customers' salaries (external counterparties).
_EMPLOYERS = [("CP-0001", "Lakeside Regional Hospital"), ("CP-0002", "Northfield School District"),
              ("CP-0003", "Brightwave Software Inc"), ("CP-0004", "Pacific Grid Electric"),
              ("CP-0005", "Summit Retail Group"), ("CP-0006", "Cedar & Pine Accounting LLP"),
              ("CP-0007", "Metro Logistics Co"), ("CP-0008", "Riverbend Pharmacy")]
_MERCHANTS = [("CP-0010", "FreshMart Grocers"), ("CP-0011", "City Power & Water"),
              ("CP-0012", "Harborview Apartments"), ("CP-0013", "QuickFuel Stations"),
              ("CP-0014", "Northline Mobile"), ("CP-0015", "Corner Pharmacy"),
              ("CP-0016", "Bayside Restaurant Supply"), ("CP-0017", "Evergreen Insurance")]
#: The ring's three related offshore companies: one registered address, one director.
_RING_ADDRESS = "Suite 4, 18 Quay Street, Douglas, Isle of Man"
_RING = [("CP-0101", "Lumen Trade Ltd"), ("CP-0102", "Coastal Import Partners Ltd"),
         ("CP-0103", "Brightline Holdings Ltd")]
#: Staff of the cafe/catering who bank elsewhere (external payees).
_OUTSIDE_STAFF = [("CP-0201", "J. Moreno"), ("CP-0202", "K. Adeyemi"), ("CP-0203", "L. Novak"),
                  ("CP-0204", "S. Haddad"), ("CP-0205", "T. Brooks"), ("CP-0206", "W. Lindgren"),
                  ("CP-0207", "Y. Castillo")]

CAFE, CATERING = "CUST-0007", "CUST-0008"
CAFE_OPERATING, CAFE_PAYROLL, CATERING_PAYROLL = "ACC-0011", "ACC-0012", "ACC-0013"
MARLOW, RHEA = "CUST-0031", "CUST-0032"
MARLOW_ACCOUNT, RHEA_ACCOUNT = "ACC-0047", "ACC-0049"
#: The NorthPeak customers on the cafe's payroll (personal accounts at the bank).
STAFF_CUSTOMERS = ("CUST-0015", "CUST-0016", "CUST-0017", "CUST-0018", "CUST-0019",
                   "CUST-0020", "CUST-0021")


@dataclass
class Dataset:
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    counterparties: dict[str, dict[str, Any]] = field(default_factory=dict)
    kyc_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    typologies: dict[str, dict[str, Any]] = field(default_factory=dict)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: (customer_a, customer_b or counterparty, reason) — relationships the bank has on record.
    relationships: list[tuple[str, str, str]] = field(default_factory=list)
    #: Earlier, closed alerts the case-management system remembers (not the planted three).
    historical_alerts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def account_transactions(self, account_id: str, from_date: str = "", to_date: str = ""
                             ) -> list[dict[str, Any]]:
        lo, hi = from_date or START.isoformat(), to_date or END.isoformat()
        return [t for t in self.transactions
                if t["account_id"] == account_id and lo <= t["booked_at"] <= hi]


def _money(rng: random.Random, low: float, high: float) -> float:
    return round(rng.uniform(low, high), 2)


def _days():
    for offset in range(DAYS):
        yield START + timedelta(days=offset)


def _paydays() -> list[date]:
    """Every other Friday in the window, ending on the last one before END."""
    fridays = [d for d in _days() if d.weekday() == 4]
    return [d for d in fridays[-1::-2][::-1] if d > START]


@lru_cache(maxsize=1)
def generate() -> Dataset:
    """The whole bank, built once. Same seed, same bank."""
    rng = random.Random(SEED)
    ds = Dataset()

    ds.typologies = {
        "TYP-FANOUT": {"typology_id": "TYP-FANOUT", "name": "Rapid fan-out (funnel account)",
                       "red_flags": "Ten or more outbound payments to distinct parties within 24 "
                                    "hours, out of line with the customer's profile."},
        "TYP-STRUCTURING": {"typology_id": "TYP-STRUCTURING", "name": "Structuring",
                            "red_flags": "Repeated cash deposits just below the $10,000 currency "
                                         "transaction report threshold."},
        "TYP-RAPID-MOVEMENT": {"typology_id": "TYP-RAPID-MOVEMENT",
                               "name": "Rapid movement of funds",
                               "red_flags": "Funds leave within 48 hours of arriving, to parties "
                                            "with no clear business purpose, often offshore."},
        "TYP-CASH-INTENSIVE": {"typology_id": "TYP-CASH-INTENSIVE",
                               "name": "Cash-intensive business",
                               "red_flags": "Cash volumes inconsistent with the stated business."},
    }

    for cp_id, name in _EMPLOYERS:
        ds.counterparties[cp_id] = {"counterparty_id": cp_id, "name": name, "kind": "employer",
                                    "country": "US", "registered_address": ""}
    for cp_id, name in _MERCHANTS:
        ds.counterparties[cp_id] = {"counterparty_id": cp_id, "name": name, "kind": "merchant",
                                    "country": "US", "registered_address": ""}
    for cp_id, name in _RING:
        ds.counterparties[cp_id] = {"counterparty_id": cp_id, "name": name, "kind": "company",
                                    "country": "IM", "registered_address": _RING_ADDRESS,
                                    "director": "M. Castellan"}
    for cp_id, name in _OUTSIDE_STAFF:
        ds.counterparties[cp_id] = {"counterparty_id": cp_id, "name": name, "kind": "individual",
                                    "country": "US", "registered_address": ""}

    # ── customers, accounts and KYC ──────────────────────────────────────────────────────────
    special = {
        CAFE: {"name": "Harbor Point Cafe LLC", "segment": "small_business",
               "occupation": "restaurant", "risk_rating": "medium"},
        CATERING: {"name": "Harbor Point Catering LLC", "segment": "small_business",
                   "occupation": "event catering", "risk_rating": "medium"},
        MARLOW: {"name": "Quentin Marlow", "segment": "retail",
                 "occupation": "sole trader, used electronics", "risk_rating": "medium"},
        RHEA: {"name": "Rhea Marlow", "segment": "retail", "occupation": "bookkeeper",
               "risk_rating": "low"},
    }
    names = [(f, l) for f in _FIRST for l in _LAST]
    rng.shuffle(names)
    for n in range(1, 41):
        cid = f"CUST-{n:04d}"
        if cid in special:
            info = dict(special[cid])
        else:
            first, last = names[n]
            info = {"name": f"{first} {last}", "segment": "retail",
                    "occupation": _OCCUPATIONS[n % len(_OCCUPATIONS)], "risk_rating": "low"}
        if cid in STAFF_CUSTOMERS:
            info["occupation"] = rng.choice(["line cook", "server", "barista", "kitchen porter"])
        onboarded = date(2016 + n % 9, 1 + n % 12, 1 + n % 27).isoformat()
        ds.customers[cid] = {"customer_id": cid, "onboarded": onboarded, **info}

    # 40 customers, 60 accounts: every customer a checking account, the specials theirs below,
    # and savings accounts for the rest until there are 60.
    acc_n = 0

    def open_account(cid: str, product: str, fixed: str = "") -> str:
        nonlocal acc_n
        acc_n += 1
        aid = fixed or f"ACC-{acc_n:04d}"
        ds.accounts[aid] = {"account_id": aid, "customer_id": cid, "product": product,
                            "opened": ds.customers[cid]["onboarded"], "status": "open"}
        return aid

    for n in range(1, 41):
        cid = f"CUST-{n:04d}"
        if cid == CAFE:
            acc_n = 10
            open_account(cid, "business_checking", CAFE_OPERATING)
            open_account(cid, "business_payroll", CAFE_PAYROLL)
        elif cid == CATERING:
            acc_n = 12
            open_account(cid, "business_payroll", CATERING_PAYROLL)
        elif cid == MARLOW:
            acc_n = 46
            open_account(cid, "personal_checking", MARLOW_ACCOUNT)
            acc_n = 47
            open_account(cid, "personal_savings", "ACC-0048")
        elif cid == RHEA:
            acc_n = 48
            open_account(cid, "personal_checking", RHEA_ACCOUNT)
        else:
            open_account(cid, "personal_checking")
    savings_for = [c for c in ds.customers if c not in special]
    i = 0
    while len(ds.accounts) < 60:
        cid = savings_for[i]
        i += 1
        aid = f"ACC-{len(ds.accounts) + 100:04d}"
        ds.accounts[aid] = {"account_id": aid, "customer_id": cid, "product": "personal_savings",
                            "opened": ds.customers[cid]["onboarded"], "status": "open"}

    employer_of: dict[str, str] = {}
    for cid, cust in ds.customers.items():
        if cid in special:
            continue
        if cid in STAFF_CUSTOMERS:
            employer_of[cid] = "Harbor Point Cafe LLC"
        else:
            employer_of[cid] = rng.choice(_EMPLOYERS)[1]
    for cid, cust in ds.customers.items():
        profile = {"customer_id": cid, "occupation": cust["occupation"],
                   "last_review": date(2026, 1 + int(cid[-2:]) % 9, 15).isoformat()}
        if cid == CAFE:
            profile.update(
                expected_activity="Card and cash takings into operating; biweekly payroll to 14 "
                                  "staff from the payroll account (every other Friday, "
                                  "$1,400-$3,000 each); suppliers monthly.",
                source_of_funds="Restaurant revenue (card and cash takings).",
                documents=["Payroll register Q3 2026 (14 employees, biweekly)",
                           "IRS EIN confirmation letter", "Business licence, City of Harborview",
                           "Beneficial owner: Dana Okafor (100%)"],
                employees=14, beneficial_owner="Dana Okafor")
        elif cid == CATERING:
            profile.update(
                expected_activity="Biweekly payroll to 9 staff (several shared with Harbor Point "
                                  "Cafe); funded from the cafe's operating account under a shared "
                                  "treasury agreement.",
                source_of_funds="Event catering contracts; intercompany funding from Harbor Point "
                                "Cafe LLC.",
                documents=["Payroll register Q3 2026 (9 employees, biweekly)",
                           "Intercompany treasury agreement with Harbor Point Cafe LLC",
                           "Beneficial owner: Dana Okafor (100%)"],
                employees=9, beneficial_owner="Dana Okafor")
        elif cid == MARLOW:
            profile.update(
                expected_activity="Personal income from a small used-electronics stall; "
                                  "expected cash deposits under $2,000 a month.",
                source_of_funds="Self-employment (market stall). No supporting accounts on file.",
                documents=["Passport", "Utility bill, 22 Mill Lane"])
        elif cid == RHEA:
            profile.update(
                expected_activity="Salary from a bookkeeping firm; household bills.",
                source_of_funds="Employment income.",
                documents=["Driver's licence", "Utility bill, 22 Mill Lane"])
        else:
            profile.update(
                expected_activity=f"Salary from {employer_of[cid]}; household spending.",
                source_of_funds="Employment income.",
                documents=["Driver's licence", "Proof of address"])
        ds.kyc_profiles[cid] = profile

    ds.relationships = [
        (CATERING, CAFE, "common beneficial owner (Dana Okafor) and shared treasury agreement"),
        (RHEA, MARLOW, "family member at the same address (22 Mill Lane)"),
        ("CP-0101", "CP-0102", "same registered address and director (M. Castellan)"),
        ("CP-0102", "CP-0103", "same registered address and director (M. Castellan)"),
        ("CP-0101", "CP-0103", "same registered address and director (M. Castellan)"),
    ]

    # ── transactions ─────────────────────────────────────────────────────────────────────────
    txns: list[dict[str, Any]] = []

    def book(account_id: str, day: date, amount: float, direction: str, channel: str,
             party: str, memo: str, counter_account: str = "") -> dict[str, Any]:
        t = {"account_id": account_id, "booked_at": day.isoformat(), "amount": round(amount, 2),
             "direction": direction, "channel": channel, "party": party, "memo": memo}
        if counter_account:
            t["counter_account_id"] = counter_account
        txns.append(t)
        return t

    def transfer(src: str, dst: str, day: date, amount: float, channel: str, memo: str) -> None:
        book(src, day, amount, "out", channel, dst, memo, counter_account=dst)
        book(dst, day, amount, "in", channel, src, memo, counter_account=src)

    # Ordinary customers: salary biweekly, rent monthly, card spending, bills.
    paydays = _paydays()
    for aid, acc in ds.accounts.items():
        cid = acc["customer_id"]
        if cid in special or acc["product"] != "personal_checking":
            continue
        if cid not in STAFF_CUSTOMERS:
            employer = next(cp for cp, name in _EMPLOYERS if name == employer_of[cid])
            salary = _money(rng, 1_700, 3_400)
            for day in paydays:
                book(aid, day, salary, "in", "ach", employer, "SALARY")
        for day in _days():
            if day.day == 1:
                book(aid, day, _money(rng, 900, 1_900), "out", "ach", "CP-0012", "RENT")
            if day.day == 5:
                book(aid, day, _money(rng, 60, 180), "out", "ach", "CP-0011", "UTILITIES")
            if day.day == 12:
                book(aid, day, _money(rng, 35, 90), "out", "card", "CP-0014", "MOBILE")
            for _ in range(rng.choice([0, 0, 1, 1, 2])):
                merchant = rng.choice(["CP-0010", "CP-0013", "CP-0015", "CP-0010"])
                book(aid, day, _money(rng, 6, 140), "out", "card", merchant, "CARD PURCHASE")
        # Savings sweeps for those who have one.
        savings = [a for a, x in ds.accounts.items()
                   if x["customer_id"] == cid and x["product"] == "personal_savings"]
        for target in savings:
            for day in paydays[1::2]:
                transfer(aid, target, day + timedelta(days=1), _money(rng, 150, 600), "transfer",
                         "SAVINGS SWEEP")

    # (a) The cafe and the catering company: takings, suppliers, and the payroll fan-outs.
    cafe_staff = list(STAFF_CUSTOMERS) + [cp for cp, _ in _OUTSIDE_STAFF]   # 7 + 7 = 14
    catering_staff = list(STAFF_CUSTOMERS[:5]) + ["CP-0205", "CP-0206", "CP-0207", "CP-0201"]
    staff_account = {cid: next(a for a, x in ds.accounts.items()
                               if x["customer_id"] == cid and x["product"] == "personal_checking")
                     for cid in STAFF_CUSTOMERS}
    pay_rate = {who: _money(rng, 1_400, 2_950) for who in cafe_staff}
    catering_rate = {who: _money(rng, 700, 1_600) for who in catering_staff}
    for day in _days():
        if day.weekday() < 6:
            book(CAFE_OPERATING, day, _money(rng, 1_800, 4_200), "in", "card",
                 "CARD SETTLEMENT", "CARD TAKINGS")
        if day.weekday() in (0, 3):
            book(CAFE_OPERATING, day, _money(rng, 600, 1_900), "in", "cash", "BRANCH",
                 "CASH TAKINGS")
        if day.day in (3, 17):
            book(CAFE_OPERATING, day, _money(rng, 2_500, 6_500), "out", "ach", "CP-0016",
                 "SUPPLIES")
    for day in paydays:
        cafe_total = sum(pay_rate.values())
        transfer(CAFE_OPERATING, CAFE_PAYROLL, day - timedelta(days=1), cafe_total, "transfer",
                 "PAYROLL FUNDING")
        for who in cafe_staff:
            if who in staff_account:
                transfer(CAFE_PAYROLL, staff_account[who], day, pay_rate[who], "ach", "PAYROLL")
            else:
                book(CAFE_PAYROLL, day, pay_rate[who], "out", "ach", who, "PAYROLL")
        catering_day = day + timedelta(days=4)   # the following Tuesday
        if catering_day <= END:
            transfer(CAFE_OPERATING, CATERING_PAYROLL, catering_day - timedelta(days=1),
                     sum(catering_rate.values()), "transfer", "INTERCOMPANY PAYROLL FUNDING")
            for who in catering_staff:
                if who in staff_account:
                    transfer(CATERING_PAYROLL, staff_account[who], catering_day,
                             catering_rate[who], "ach", "PAYROLL HPC CATERING")
                else:
                    book(CATERING_PAYROLL, catering_day, catering_rate[who], "out", "ach", who,
                         "PAYROLL HPC CATERING")

    # (b) The ring: sub-threshold cash in, wires out to the three companies, money back to Rhea.
    for day in _days():
        if day.weekday() in (0, 2, 4) and day >= START + timedelta(days=20):
            deposit = _money(rng, 9_050, 9_950)
            book(MARLOW_ACCOUNT, day, deposit, "in", "cash", "BRANCH", "CASH DEPOSIT")
        if day.weekday() == 1 and day >= START + timedelta(days=21):
            for cp_id, _ in _RING:
                book(MARLOW_ACCOUNT, day, _money(rng, 8_400, 9_800), "out", "wire", cp_id,
                     "INVOICE PAYMENT")
        if day.weekday() == 3 and day >= START + timedelta(days=28):
            book(RHEA_ACCOUNT, day, _money(rng, 6_000, 8_900), "in", "wire", "CP-0103",
                 "CONSULTING FEE")
        if day.day in (2, 16):
            book(MARLOW_ACCOUNT, day, _money(rng, 300, 700), "out", "card", "CP-0010",
                 "CARD PURCHASE")
            book(RHEA_ACCOUNT, day, _money(rng, 1_900, 2_300), "in", "ach", "CP-0006", "SALARY")

    txns.sort(key=lambda t: (t["booked_at"], t["account_id"], t["direction"], t["party"],
                             t["amount"]))
    for n, t in enumerate(txns, start=1):
        t["transaction_id"] = f"TXN-{n:06d}"
    ds.transactions = txns

    # ── alerts ───────────────────────────────────────────────────────────────────────────────
    last_cafe_payday = paydays[-1].isoformat()
    cafe_batch = [t["transaction_id"] for t in txns if t["account_id"] == CAFE_PAYROLL
                  and t["booked_at"] == last_cafe_payday and t["direction"] == "out"]
    catering_days = sorted({t["booked_at"] for t in txns if t["account_id"] == CATERING_PAYROLL
                            and t["direction"] == "out"})
    catering_batch = [t["transaction_id"] for t in txns if t["account_id"] == CATERING_PAYROLL
                      and t["booked_at"] == catering_days[-1] and t["direction"] == "out"]
    last_two_weeks = (END - timedelta(days=13)).isoformat()
    ring_flags = [t["transaction_id"] for t in txns if t["account_id"] == MARLOW_ACCOUNT
                  and t["booked_at"] >= last_two_weeks and t["channel"] in ("cash", "wire")]
    ds.alerts = {
        "ALERT-1": {"alert_id": "ALERT-1", "rule": "FANOUT-01",
                    "rule_text": "10 or more outbound payments to distinct parties within 24 hours",
                    "score": 71, "raised_at": last_cafe_payday, "status": "open",
                    "account_id": CAFE_PAYROLL, "customer_id": CAFE,
                    "typology_ids": ["TYP-FANOUT"], "flagged_transactions": cafe_batch},
        "ALERT-2": {"alert_id": "ALERT-2", "rule": "FANOUT-01",
                    "rule_text": "10 or more outbound payments to distinct parties within 24 hours "
                                 "(9 payments; scored up by the intercompany funding the day "
                                 "before)",
                    "score": 64, "raised_at": catering_days[-1], "status": "open",
                    "account_id": CATERING_PAYROLL, "customer_id": CATERING,
                    "typology_ids": ["TYP-FANOUT"], "flagged_transactions": catering_batch},
        "ALERT-3": {"alert_id": "ALERT-3", "rule": "STRUCT-02",
                    "rule_text": "3 or more cash deposits between $9,000 and $9,999 within 7 days, "
                                 "followed by outbound wires within 48 hours",
                    "score": 92, "raised_at": END.isoformat(), "status": "open",
                    "account_id": MARLOW_ACCOUNT, "customer_id": MARLOW,
                    "typology_ids": ["TYP-STRUCTURING", "TYP-RAPID-MOVEMENT"],
                    "flagged_transactions": ring_flags},
    }
    ds.historical_alerts = {
        "ALERT-0412": {"alert_id": "ALERT-0412", "rule": "VELOCITY-03", "score": 38,
                       "raised_at": "2026-03-11", "status": "closed", "account_id": "ACC-0005",
                       "customer_id": "CUST-0005", "typology_ids": [],
                       "flagged_transactions": [],
                       "determination": "closed_false_positive",
                       "reason": "Card dispute refunds; merchant confirmed."},
        "ALERT-0577": {"alert_id": "ALERT-0577", "rule": "CASH-01", "score": 44,
                       "raised_at": "2026-05-02", "status": "closed", "account_id": "ACC-0029",
                       "customer_id": "CUST-0024", "typology_ids": ["TYP-CASH-INTENSIVE"],
                       "flagged_transactions": [],
                       "determination": "closed_false_positive",
                       "reason": "Proceeds of a documented private car sale."},
    }
    return ds


def summarize(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """What an analyst reads first about a set of transactions."""
    ins = [t for t in transactions if t["direction"] == "in"]
    outs = [t for t in transactions if t["direction"] == "out"]
    near_ctr = [t for t in ins if t["channel"] == "cash"
                and 9_000 <= t["amount"] < CTR_THRESHOLD]
    by_party: dict[str, dict[str, Any]] = {}
    for t in outs:
        row = by_party.setdefault(t["party"], {"party": t["party"], "count": 0, "total": 0.0})
        row["count"] += 1
        row["total"] = round(row["total"] + t["amount"], 2)
    top = sorted(by_party.values(), key=lambda r: -r["total"])[:8]
    return {"count": len(transactions), "inbound_count": len(ins), "outbound_count": len(outs),
            "inbound_total": round(sum(t["amount"] for t in ins), 2),
            "outbound_total": round(sum(t["amount"] for t in outs), 2),
            "cash_deposits": sum(1 for t in ins if t["channel"] == "cash"),
            "cash_deposits_9000_to_9999": len(near_ctr),
            "wires_out": sum(1 for t in outs if t["channel"] == "wire"),
            "distinct_payees": len(by_party), "top_payees": top}
