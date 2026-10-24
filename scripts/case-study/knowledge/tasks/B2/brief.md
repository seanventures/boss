# B2 · Take the loan fee and charge rules out of the loan module

**From:** NorthPeak Bank, core-banking modernisation lead · **Repository:** `northpeak-fineract` (our Apache Fineract fork)

The interest rules now have a service of their own. Fees and penalties are next: they are the
rule family our customers complain about most, and the rules are spread over several classes
in the loan module, some of them written twice.

Please move the fee and charge calculation into one service. Like the interest work, this is
a pure refactor: **every charge amount must stay exactly as it is today.**

What matters to us:
- Flat charges, and percentage charges of the loan amount, of amount plus interest, of interest, or of the disbursed amount.
- Charges at disbursement, on a specified due date, per instalment, and on overdue instalments.
- Minimum and maximum caps.
- Rounding to the currency, for every currency we lend in, including ones with no decimals and with three.
- The split of instalment fees across the schedule, and which instalment a dated charge lands on.
- One rule, in one place: where the same calculation is written twice today, keep a single copy.

Tell us, for the change record, which charge rules you found and whether anything you learned
moving the interest rules applied here too.

## Contract
- New public class `org.apache.fineract.portfolio.loanaccount.service.ChargeCalculationService` in `fineract-loan`. It owns the base amount a percentage applies to, the percentage itself, the min/max cap, and the per-installment fee amount.
- `SingleLoanChargeRepaymentScheduleProcessingWrapper` no longer declares `getBaseAmount` or `getInstallmentFee`; `LoanChargeService` no longer declares `calculateInstallmentChargeAmount` or `getDerivedAmountForCharge`. Both use the new service.
- Public API unchanged: `LoanChargeService`'s public constructor and public methods, and `new SingleLoanChargeRepaymentScheduleProcessingWrapper()` with `reprocess(…)`, keep their signatures.
- Every charge amount, base amount, instalment split and schedule fee/penalty portion is unchanged to the smallest currency unit.
- `./gradlew :fineract-loan:test` passes.
