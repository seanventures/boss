# B1 · Take the loan interest rules out of the schedule engine

**From:** NorthPeak Bank, core-banking modernisation lead · **Repository:** `northpeak-fineract` (our Apache Fineract fork)

We are breaking our core banking platform into services, one rule family at a time, and loan
interest is first. Today the interest rules for our progressive (EMI) loans live inside the
schedule engine, mixed in with instalment and repayment logic. Nobody can review them on their
own, and every change to the engine risks changing what customers are charged.

Please move the interest calculation into a service of its own. This is a pure refactor:
**not one cent may change.** Every repayment schedule we generate today must come out
identical afterwards, for every product we sell.

What matters to us:
- All day-count conventions keep working exactly as they do: actual days, 360, 364 and 365 days a year, actual or 30-day months, and the leap-year options (full leap year, or 366 only for a period that contains 29 February).
- Rounding stays the same: currency decimals, the bank's rounding mode, and instalments rounded to multiples where a product asks for it.
- Grace periods behave as before: principal grace (interest-only instalments) and interest grace.
- Early repayments still recalculate interest the same way.
- Leave the code easier to review: the new service should hold the interest rules and nothing else, and it should have its own tests.

Tell us, for the change record, which business rules you found in the interest code and where
each now lives.

## Contract
- New public class `org.apache.fineract.portfolio.loanproduct.calc.InterestCalculationService` in `fineract-progressive-loan`. It owns the day-count rules (days in year and days in month, including the leap-year strategies) and the interest rate-factor rules.
- `ProgressiveEMICalculator` delegates to it through an instance field, and no longer declares the day-count or rate-factor methods itself (`getNumberOfDays`, `calculateRateFactorPerPeriod`, `calculatePeriodFractions`, the `rateFactorByRepayment…` family and their helpers).
- Public API unchanged: the `EMICalculator` interface, `new ProgressiveEMICalculator(ScheduledDateGenerator)` and `ProgressiveLoanScheduleGenerator` keep their signatures.
- Every schedule, accrual and early-repayment recalculation produced by `EMICalculator` and `ProgressiveLoanScheduleGenerator` is unchanged to the cent.
- `./gradlew :fineract-progressive-loan:test` passes.
