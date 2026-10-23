# B3 · Incident: repayments post more than the customer paid

**From:** NorthPeak Bank, payments operations (on call) · **Repository:** `northpeak-fineract` (the release that went out yesterday)

**Incident INC-2041 · Severity 2 · opened 06:40**

**What we see.** Overnight, the end-of-day batch posted the day's standing-order loan
repayments. This morning reconciliation fails. For loans on our standard repayment strategy
(penalties, fees, interest, then principal), the principal and interest the ledger booked add
up to more than the cash we received. Three examples from the reconciliation report:

| Loan | Cash received | Interest booked | Principal booked |
|---|---|---|---|
| L-118204 | 250.00 | 31.40 | 250.00 |
| L-118377 | 1,200.00 | 96.15 | 1,200.00 |
| L-120015 | 89.90 | 12.06 | 89.90 |

Customers' outstanding balances have dropped by more than they paid, and some later
instalments now show as partly paid although nothing was paid towards them. Loans on other
strategies look fine so far. Nothing changed in the payment channel; the only change was
yesterday's platform release.

**What we need.**
- Find the cause and fix it, so that every repayment is posted exactly once: the parts it is split into must add up to the cash received.
- Add a test that would have caught this.
- Give us the root cause in one sentence for the incident record, and a short runbook entry: how to spot this class of fault quickly next time.

## Contract
- For a repayment on any date (before, on or after an instalment's due date), the transaction's principal, interest, fee and penalty portions plus any overpayment equal the amount received.
- Across a loan, the amounts the instalments record as paid equal the cash applied to them, and no component is ever paid beyond what was charged.
- Under the `mifos-standard-strategy` (penalties, fees, interest, principal order) each instalment, oldest first, is paid penalty, then fee, then interest, then principal.
- `./gradlew :fineract-loan:test` passes.
