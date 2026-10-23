# B4 · Incident: early repayments post more than the customer paid

**From:** NorthPeak Bank, payments operations (on call) · **Repository:** `northpeak-fineract` (the release that went out yesterday)

**Incident INC-2088 · Severity 2 · opened 07:15**

**What we see.** Reconciliation fails again, on a different product. Our consumer instalment
loans use the strategy that pays what is due first (penalties, fees, interest, principal) and,
for money paid ahead of the due date, principal first. Since yesterday's release, when a
customer pays **before** their instalment is due, the ledger books more than the cash received:

| Loan | Paid on | Next due | Cash received | Booked (principal + interest + fees) |
|---|---|---|---|---|
| C-55102 | 3 Mar | 15 Mar | 400.00 | 471.20 |
| C-55340 | 9 Mar | 31 Mar | 150.00 | 166.75 |

Repayments made on or after the due date reconcile. Some customers' next instalments show as
paid that they have not paid. The standard strategy that broke in last week's incident is fine.

**What we need.**
- Find the cause and fix it: every repayment, early or not, is posted exactly once.
- Add a test that would have caught this.
- The root cause in one sentence for the incident record, and whether it is related to anything we have seen before.

## Contract
- For a repayment on any date, the transaction's principal, interest, fee and penalty portions plus any overpayment equal the amount received.
- Across a loan, the amounts the instalments record as paid equal the cash applied to them, and no component is ever paid beyond what was charged.
- Under the `due-penalty-fee-interest-principal-in-advance-principal-penalty-fee-interest-strategy`, an instalment that is due or late is paid penalty, fee, interest, principal; an instalment paid before its due date is paid principal first, then penalty, fee, interest.
- `./gradlew :fineract-loan:test` passes.
