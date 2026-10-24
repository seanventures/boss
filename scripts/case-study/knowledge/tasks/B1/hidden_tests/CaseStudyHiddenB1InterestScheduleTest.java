/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.fineract.portfolio.loanproduct.calc;

import static org.mockito.Mockito.mock;

import java.io.IOException;
import java.io.InputStream;
import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Supplier;
import java.util.stream.Stream;
import org.apache.fineract.organisation.monetary.data.CurrencyData;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.domain.MoneyHelper;
import org.apache.fineract.portfolio.common.domain.DaysInMonthType;
import org.apache.fineract.portfolio.common.domain.DaysInYearCustomStrategyType;
import org.apache.fineract.portfolio.common.domain.DaysInYearType;
import org.apache.fineract.portfolio.common.domain.PeriodFrequencyType;
import org.apache.fineract.portfolio.loanaccount.loanschedule.data.LoanSchedulePlan;
import org.apache.fineract.portfolio.loanaccount.loanschedule.data.LoanSchedulePlanDisbursementPeriod;
import org.apache.fineract.portfolio.loanaccount.loanschedule.data.LoanSchedulePlanPeriod;
import org.apache.fineract.portfolio.loanaccount.loanschedule.data.LoanSchedulePlanRepaymentPeriod;
import org.apache.fineract.portfolio.loanaccount.loanschedule.domain.DefaultScheduledDateGenerator;
import org.apache.fineract.portfolio.loanaccount.loanschedule.domain.LoanRepaymentScheduleModelData;
import org.apache.fineract.portfolio.loanaccount.loanschedule.domain.LoanScheduleModelRepaymentPeriod;
import org.apache.fineract.portfolio.loanaccount.loanschedule.domain.LoanScheduleProcessingType;
import org.apache.fineract.portfolio.loanaccount.loanschedule.domain.ProgressiveLoanScheduleGenerator;
import org.apache.fineract.portfolio.loanaccount.service.InterestScheduleModelRepositoryWrapper;
import org.apache.fineract.portfolio.loanaccount.service.LoanTransactionProcessingService;
import org.apache.fineract.portfolio.loanproduct.calc.data.PeriodDueDetails;
import org.apache.fineract.portfolio.loanproduct.calc.data.ProgressiveLoanInterestScheduleModel;
import org.apache.fineract.portfolio.loanproduct.calc.data.RepaymentPeriod;
import org.apache.fineract.portfolio.loanproduct.domain.AmortizationMethod;
import org.apache.fineract.portfolio.loanproduct.domain.ILoanConfigurationDetails;
import org.apache.fineract.portfolio.loanproduct.domain.InterestCalculationPeriodMethod;
import org.apache.fineract.portfolio.loanproduct.domain.InterestMethod;
import org.apache.fineract.portfolio.loanproduct.domain.LoanPreCloseInterestCalculationStrategy;
import org.apache.fineract.portfolio.loanproduct.domain.RecalculationFrequencyType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.mockito.MockedStatic;
import org.mockito.Mockito;

/**
 * Case study B1 (hidden acceptance suite): characterisation of the progressive loan interest calculation.
 *
 * <p>
 * Generates a deterministic set of loans across day-count conventions (days in year / days in month, the leap-year
 * strategies), repayment frequencies, rounding (currency digits, rounding mode, installment multiples), principal and
 * interest grace, interest methods and period methods, and compares every repayment schedule, daily accrual probe and
 * early-repayment recalculation with the schedules recorded on the pinned Fineract release (the "old" behaviour). A
 * refactor that keeps behaviour passes; any change to a single cent fails the loan it touches.
 *
 * <p>
 * Uses only the public API that the B1 brief keeps stable: {@link EMICalculator} through
 * {@code new ProgressiveEMICalculator(ScheduledDateGenerator)}, and {@link ProgressiveLoanScheduleGenerator}.
 *
 * <p>
 * Record the golden file (on the pinned release only): {@code CASE_STUDY_RECORD_DIR=/some/dir ./gradlew
 * :fineract-progressive-loan:test --tests '*CaseStudyHiddenB1*'}.
 */
class CaseStudyHiddenB1InterestScheduleTest {

    private static final String GOLDEN = "case-study/b1_interest_schedules.golden";
    private static final long SEED = 20261022L;
    private static final int LOANS_EMI = 160;
    private static final int LOANS_PLAN = 40;

    private static MockedStatic<MoneyHelper> moneyHelper;
    private static RoundingMode roundingMode = RoundingMode.HALF_EVEN;
    private static Map<String, List<String>> golden;

    @BeforeAll
    static void init() throws IOException {
        moneyHelper = Mockito.mockStatic(MoneyHelper.class);
        moneyHelper.when(MoneyHelper::getRoundingMode).thenAnswer(i -> roundingMode);
        moneyHelper.when(MoneyHelper::getMathContext).thenAnswer(i -> new MathContext(12, roundingMode));
        golden = readGolden();
    }

    @AfterAll
    static void tearDown() throws IOException {
        moneyHelper.close();
    }

    @TestFactory
    Stream<DynamicTest> interestSchedulesMatchThePinnedRelease() throws IOException {
        Map<String, List<String>> actual = new LinkedHashMap<>();
        Random random = new Random(SEED);
        for (int i = 0; i < LOANS_EMI; i++) {
            LoanCase loan = LoanCase.generate(random, i);
            actual.put(loan.id(), capture(() -> emiScenario(loan)));
        }
        for (int i = 0; i < LOANS_PLAN; i++) {
            PlanCase plan = PlanCase.generate(random, i);
            actual.put(plan.id(), capture(() -> planScenario(plan)));
        }
        record(actual);
        return actual.entrySet().stream().map(e -> DynamicTest.dynamicTest(e.getKey(), () -> {
            Assertions.assertNotNull(golden, "golden file " + GOLDEN + " is missing from the test classpath");
            List<String> expected = golden.get(e.getKey());
            Assertions.assertNotNull(expected, "no recorded schedule for " + e.getKey());
            Assertions.assertEquals(String.join("\n", expected), String.join("\n", e.getValue()),
                    "schedule differs from the pinned release for " + e.getKey());
        }));
    }

    // --- scenarios ------------------------------------------------------------------------------------------------

    private static List<String> emiScenario(LoanCase loan) {
        roundingMode = loan.roundingMode;
        MathContext mc = new MathContext(12, roundingMode);
        EMICalculator calculator = new ProgressiveEMICalculator(new DefaultScheduledDateGenerator());
        List<String> out = new ArrayList<>();
        out.add("loan " + loan.describe());

        ProgressiveLoanInterestScheduleModel model = calculator.generatePeriodInterestScheduleModel(loan.periods(mc), loan, loan.multiplesOf,
                mc);
        calculator.addDisbursement(model, loan.disbursementDate, money(loan.currency, loan.principal));
        snapshot("schedule", model, out);

        // Daily accrual probes: a third of the way into the first period and the middle of the last one.
        RepaymentPeriod first = model.repaymentPeriods().getFirst();
        RepaymentPeriod last = model.repaymentPeriods().getLast();
        out.add("accrual first " + amount(calculator.getPeriodInterestTillDate(model, first.getFromDate(), first.getDueDate(),
                between(first.getFromDate(), first.getDueDate(), 3), false, false)));
        out.add("accrual last " + amount(calculator.getPeriodInterestTillDate(model, last.getFromDate(), last.getDueDate(),
                between(last.getFromDate(), last.getDueDate(), 2), false, false)));

        // Early repayment of the first installment, a few days before it is due: interest is recalculated.
        LocalDate payDate = first.getDueDate().minusDays(Math.min(5, Math.max(0, daysIn(first) - 1)));
        PeriodDueDetails due = calculator.getDueAmounts(model, first.getFromDate(), first.getDueDate(), payDate);
        out.add("due-at-pay " + amount(due.getEmi()) + " " + amount(due.getDueInterest()) + " " + amount(due.getDuePrincipal()));
        calculator.payInterest(model, first.getFromDate(), first.getDueDate(), payDate, due.getDueInterest());
        calculator.payPrincipal(model, first.getFromDate(), first.getDueDate(), payDate, due.getDuePrincipal());
        snapshot("after-early-pay", model, out);
        out.add("outstanding-interest-at-maturity " + amount(calculator.getOutstandingInterestTillDate(model, last.getDueDate())));
        return out;
    }

    private static List<String> planScenario(PlanCase plan) {
        roundingMode = plan.roundingMode;
        MathContext mc = new MathContext(12, roundingMode);
        ProgressiveLoanScheduleGenerator generator = new ProgressiveLoanScheduleGenerator(new DefaultScheduledDateGenerator(),
                new ProgressiveEMICalculator(new DefaultScheduledDateGenerator()), mock(InterestScheduleModelRepositoryWrapper.class));
        generator.setLoanTransactionProcessingService(mock(LoanTransactionProcessingService.class));
        LoanSchedulePlan schedule = generator.generate(mc, plan.modelData());
        List<String> out = new ArrayList<>();
        out.add("plan " + plan.describe());
        out.add("totals term=" + schedule.getLoanTermInDays() + " disbursed=" + plain(schedule.getTotalDisbursedAmount()) + " principal="
                + plain(schedule.getTotalPrincipalAmount()) + " interest=" + plain(schedule.getTotalInterestAmount()) + " repayment="
                + plain(schedule.getTotalRepaymentAmount()));
        for (LoanSchedulePlanPeriod period : schedule.getPeriods()) {
            if (period instanceof LoanSchedulePlanRepaymentPeriod rp) {
                out.add("R" + rp.periodNumber() + " " + rp.periodFromDate() + ".." + rp.periodDueDate() + " p=" + plain(rp.getPrincipalAmount())
                        + " i=" + plain(rp.getInterestAmount()) + " due=" + plain(rp.getTotalDueAmount()) + " bal="
                        + plain(rp.getOutstandingLoanBalance()));
            } else if (period instanceof LoanSchedulePlanDisbursementPeriod dp) {
                out.add("D " + dp.periodDueDate() + " p=" + plain(dp.getPrincipalAmount()));
            } else {
                out.add(period.getClass().getSimpleName() + " " + period.periodNumber() + " " + period.periodFromDate() + ".."
                        + period.periodDueDate());
            }
        }
        return out;
    }

    private static void snapshot(String label, ProgressiveLoanInterestScheduleModel model, List<String> out) {
        out.add(label + " totalDueInterest=" + amount(model.getTotalDueInterest()) + " totalDuePrincipal="
                + amount(model.getTotalDuePrincipal()));
        int n = 1;
        for (RepaymentPeriod rp : model.repaymentPeriods()) {
            out.add("  #" + n++ + " " + rp.getFromDate() + ".." + rp.getDueDate() + " emi=" + amount(rp.getEmi()) + " i="
                    + amount(rp.getDueInterest()) + " p=" + amount(rp.getDuePrincipal()) + " bal=" + amount(rp.getOutstandingLoanBalance())
                    + (rp.isFullyPaid() ? " paid" : ""));
        }
    }

    // --- generated loans ----------------------------------------------------------------------------------------------

    private static final LocalDate[] DISBURSEMENT_DATES = { LocalDate.of(2024, 1, 1), LocalDate.of(2024, 1, 15), LocalDate.of(2024, 1, 31),
            LocalDate.of(2024, 2, 29), LocalDate.of(2024, 3, 31), LocalDate.of(2023, 11, 30), LocalDate.of(2023, 12, 15),
            LocalDate.of(2024, 12, 20), LocalDate.of(2025, 1, 30), LocalDate.of(2025, 2, 28), LocalDate.of(2027, 12, 31),
            LocalDate.of(2028, 2, 1) };

    private static final CurrencyData[] CURRENCIES = { new CurrencyData("USD", "US Dollar", 2, 1, "$", "USD"),
            new CurrencyData("JPY", "Yen", 0, 1, "¥", "JPY"), new CurrencyData("KWD", "Dinar", 3, 1, "KD", "KWD"),
            new CurrencyData("EUR", "Euro", 2, 5, "€", "EUR") };

    private static final RoundingMode[] ROUNDING = { RoundingMode.HALF_EVEN, RoundingMode.HALF_UP, RoundingMode.HALF_DOWN };

    /** One generated loan, also serving as its own product configuration. */
    static final class LoanCase implements ILoanConfigurationDetails {

        String id;
        CurrencyData currency;
        RoundingMode roundingMode;
        Integer multiplesOf;
        LocalDate disbursementDate;
        BigDecimal principal;
        BigDecimal rate;
        int repayments;
        int repayEvery;
        PeriodFrequencyType frequency;
        DaysInYearType daysInYear;
        DaysInMonthType daysInMonth;
        DaysInYearCustomStrategyType customStrategy;
        InterestMethod interestMethod;
        InterestCalculationPeriodMethod periodMethod;
        boolean partialPeriod;
        boolean recognitionOnDisbursement;
        int graceOnPrincipal;
        int graceOnInterest;

        static LoanCase generate(Random r, int index) {
            LoanCase c = new LoanCase();
            c.id = String.format("emi-%03d", index);
            c.currency = CURRENCIES[r.nextInt(CURRENCIES.length)];
            c.roundingMode = ROUNDING[r.nextInt(ROUNDING.length)];
            c.multiplesOf = switch (r.nextInt(5)) {
                case 0 -> 1;
                case 1 -> 5;
                default -> null;
            };
            c.disbursementDate = DISBURSEMENT_DATES[r.nextInt(DISBURSEMENT_DATES.length)];
            c.principal = BigDecimal.valueOf(10_000 + r.nextInt(25_000_000), 2);
            c.rate = BigDecimal.valueOf(50 + r.nextInt(3_550), 2);
            int f = r.nextInt(10);
            c.frequency = f < 6 ? PeriodFrequencyType.MONTHS : f < 8 ? PeriodFrequencyType.WEEKS : PeriodFrequencyType.DAYS;
            c.repayEvery = switch (c.frequency) {
                case MONTHS -> 1 + (r.nextInt(4) == 0 ? 1 + r.nextInt(2) : 0);
                case WEEKS -> 1 + r.nextInt(2);
                default -> r.nextBoolean() ? 15 : 30;
            };
            c.repayments = 1 + r.nextInt(c.frequency == PeriodFrequencyType.MONTHS ? 24 : 12);
            c.daysInYear = new DaysInYearType[] { DaysInYearType.ACTUAL, DaysInYearType.DAYS_360, DaysInYearType.DAYS_364,
                    DaysInYearType.DAYS_365 }[r.nextInt(4)];
            c.daysInMonth = r.nextBoolean() ? DaysInMonthType.ACTUAL : DaysInMonthType.DAYS_30;
            c.customStrategy = c.daysInYear == DaysInYearType.ACTUAL
                    ? new DaysInYearCustomStrategyType[] { null, DaysInYearCustomStrategyType.FULL_LEAP_YEAR,
                            DaysInYearCustomStrategyType.FEB_29_PERIOD_ONLY }[r.nextInt(3)]
                    : null;
            c.interestMethod = r.nextInt(5) == 0 ? InterestMethod.FLAT : InterestMethod.DECLINING_BALANCE;
            c.periodMethod = r.nextInt(4) == 0 ? InterestCalculationPeriodMethod.SAME_AS_REPAYMENT_PERIOD
                    : InterestCalculationPeriodMethod.DAILY;
            c.partialPeriod = r.nextInt(4) != 0;
            c.recognitionOnDisbursement = r.nextInt(3) == 0;
            c.graceOnPrincipal = c.repayments > 2 && r.nextInt(3) == 0 ? 1 + r.nextInt(Math.min(3, c.repayments - 2)) : 0;
            c.graceOnInterest = c.repayments > 2 && r.nextInt(4) == 0 ? 1 + r.nextInt(Math.min(2, c.repayments - 2)) : 0;
            return c;
        }

        String id() {
            return id;
        }

        String describe() {
            return id + " " + currency.getCode() + "/" + currency.getDecimalPlaces() + "/" + currency.getInMultiplesOf() + " " + roundingMode
                    + " multiples=" + multiplesOf + " disb=" + disbursementDate + " principal=" + principal.toPlainString() + " rate="
                    + rate.toPlainString() + " n=" + repayments + " every=" + repayEvery + " " + frequency + " " + daysInYear + "/"
                    + daysInMonth + " custom=" + customStrategy + " " + interestMethod + " " + periodMethod + " partial=" + partialPeriod
                    + " recognitionOnDisb=" + recognitionOnDisbursement + " graceP=" + graceOnPrincipal + " graceI=" + graceOnInterest;
        }

        List<LoanScheduleModelRepaymentPeriod> periods(MathContext mc) {
            List<LoanScheduleModelRepaymentPeriod> periods = new ArrayList<>();
            LocalDate from = disbursementDate;
            for (int i = 1; i <= repayments; i++) {
                LocalDate due = switch (frequency) {
                    case MONTHS -> disbursementDate.plusMonths((long) i * repayEvery);
                    case WEEKS -> disbursementDate.plusWeeks((long) i * repayEvery);
                    default -> disbursementDate.plusDays((long) i * repayEvery);
                };
                periods.add(LoanScheduleModelRepaymentPeriod.repayment(i, from, due, null, null, null, null, null, null, false, mc));
                from = due;
            }
            return periods;
        }

        @Override
        public CurrencyData getCurrencyData() {
            return currency;
        }

        @Override
        public Integer getGraceOnInterestCharged() {
            return 0;
        }

        @Override
        public Integer getGraceOnInterestPayment() {
            return graceOnInterest;
        }

        @Override
        public Integer getGraceOnPrincipalPayment() {
            return graceOnPrincipal;
        }

        @Override
        public Integer getRecurringMoratoriumOnPrincipalPeriods() {
            return 0;
        }

        @Override
        public BigDecimal getNominalInterestRatePerPeriod() {
            return rate;
        }

        @Override
        public PeriodFrequencyType getInterestPeriodFrequencyType() {
            return PeriodFrequencyType.YEARS;
        }

        @Override
        public BigDecimal getAnnualNominalInterestRate() {
            return rate;
        }

        @Override
        public InterestMethod getInterestMethod() {
            return interestMethod;
        }

        @Override
        public InterestCalculationPeriodMethod getInterestCalculationPeriodMethod() {
            return periodMethod;
        }

        @Override
        public boolean isAllowPartialPeriodInterestCalculation() {
            return partialPeriod;
        }

        @Override
        public Integer getRepayEvery() {
            return repayEvery;
        }

        @Override
        public PeriodFrequencyType getRepaymentPeriodFrequencyType() {
            return frequency;
        }

        @Override
        public Integer getNumberOfRepayments() {
            return repayments;
        }

        @Override
        public AmortizationMethod getAmortizationMethod() {
            return AmortizationMethod.EQUAL_INSTALLMENTS;
        }

        @Override
        public Integer getGraceOnArrearsAgeing() {
            return 0;
        }

        @Override
        public Integer getDaysInMonthType() {
            return daysInMonth.getValue();
        }

        @Override
        public Integer getDaysInYearType() {
            return daysInYear.getValue();
        }

        @Override
        public boolean isInterestRecognitionOnDisbursementDate() {
            return recognitionOnDisbursement;
        }

        @Override
        public DaysInYearCustomStrategyType getDaysInYearCustomStrategy() {
            return customStrategy;
        }

        @Override
        public boolean isInterestRecalculationEnabled() {
            return true;
        }

        @Override
        public RecalculationFrequencyType getRestFrequencyType() {
            return RecalculationFrequencyType.DAILY;
        }

        @Override
        public LoanPreCloseInterestCalculationStrategy getPreCloseInterestCalculationStrategy() {
            return LoanPreCloseInterestCalculationStrategy.TILL_PRE_CLOSURE_DATE;
        }

        @Override
        public boolean isAllowFullTermForTranche() {
            return false;
        }

        @Override
        public LoanScheduleProcessingType getLoanScheduleProcessingType() {
            return LoanScheduleProcessingType.HORIZONTAL;
        }
    }

    /** A generated loan for the full schedule generator (the path the API's schedule preview uses). */
    record PlanCase(String id, CurrencyData currency, RoundingMode roundingMode, LocalDate disbursementDate, BigDecimal principal,
            BigDecimal rate, int repayments, int repayEvery, String frequency, DaysInYearType daysInYear, DaysInMonthType daysInMonth,
            DaysInYearCustomStrategyType customStrategy, boolean downPayment, Integer multiplesOf, InterestMethod interestMethod,
            boolean partialPeriod) {

        static PlanCase generate(Random r, int index) {
            String frequency = new String[] { "MONTHS", "MONTHS", "WEEKS", "DAYS" }[r.nextInt(4)];
            DaysInYearType diy = new DaysInYearType[] { DaysInYearType.ACTUAL, DaysInYearType.DAYS_360, DaysInYearType.DAYS_364,
                    DaysInYearType.DAYS_365 }[r.nextInt(4)];
            return new PlanCase(String.format("plan-%03d", index), CURRENCIES[r.nextInt(CURRENCIES.length)],
                    ROUNDING[r.nextInt(ROUNDING.length)], DISBURSEMENT_DATES[r.nextInt(DISBURSEMENT_DATES.length)],
                    BigDecimal.valueOf(10_000 + r.nextInt(5_000_000), 2), BigDecimal.valueOf(100 + r.nextInt(2_900), 2),
                    1 + r.nextInt(18), "DAYS".equals(frequency) ? 15 : 1, frequency, diy,
                    r.nextBoolean() ? DaysInMonthType.ACTUAL : DaysInMonthType.DAYS_30,
                    diy == DaysInYearType.ACTUAL && r.nextBoolean() ? DaysInYearCustomStrategyType.FEB_29_PERIOD_ONLY : null,
                    r.nextInt(4) == 0, r.nextInt(4) == 0 ? 1 : null, r.nextInt(5) == 0 ? InterestMethod.FLAT : InterestMethod.DECLINING_BALANCE,
                    r.nextBoolean());
        }

        LoanRepaymentScheduleModelData modelData() {
            return new LoanRepaymentScheduleModelData(disbursementDate, currency, principal, disbursementDate, repayments, repayEvery,
                    frequency, rate, downPayment, daysInMonth, daysInYear, downPayment ? BigDecimal.valueOf(25) : null, multiplesOf, null,
                    false, customStrategy, interestMethod, partialPeriod, false);
        }

        String describe() {
            return id + " " + currency.getCode() + " " + roundingMode + " disb=" + disbursementDate + " principal=" + principal.toPlainString()
                    + " rate=" + rate.toPlainString() + " n=" + repayments + " every=" + repayEvery + " " + frequency + " " + daysInYear + "/"
                    + daysInMonth + " custom=" + customStrategy + " down=" + downPayment + " multiples=" + multiplesOf + " "
                    + interestMethod + " partial=" + partialPeriod;
        }
    }

    // --- helpers ------------------------------------------------------------------------------------------------------

    /** Behaviour includes failures: a loan the release rejects must still be rejected the same way. */
    private static List<String> capture(Supplier<List<String>> scenario) {
        try {
            return scenario.get();
        } catch (RuntimeException e) {
            return List.of("ERROR " + e.getClass().getName());
        }
    }

    private static Money money(CurrencyData currency, BigDecimal amount) {
        return Money.of(currency, amount, new MathContext(12, roundingMode));
    }

    private static String amount(Money money) {
        return money == null ? "null" : plain(money.getAmount());
    }

    private static String plain(BigDecimal value) {
        return value == null ? "null" : value.stripTrailingZeros().toPlainString();
    }

    private static int daysIn(RepaymentPeriod period) {
        return (int) java.time.temporal.ChronoUnit.DAYS.between(period.getFromDate(), period.getDueDate());
    }

    private static LocalDate between(LocalDate from, LocalDate to, int divisor) {
        return from.plusDays(java.time.temporal.ChronoUnit.DAYS.between(from, to) / divisor);
    }

    private static Map<String, List<String>> readGolden() throws IOException {
        try (InputStream in = CaseStudyHiddenB1InterestScheduleTest.class.getClassLoader().getResourceAsStream(GOLDEN)) {
            if (in == null) {
                return null;
            }
            return parse(new String(in.readAllBytes(), StandardCharsets.UTF_8));
        }
    }

    static Map<String, List<String>> parse(String text) {
        Map<String, List<String>> out = new LinkedHashMap<>();
        List<String> current = null;
        for (String line : text.split("\n", -1)) {
            if (line.startsWith("=== ")) {
                current = new ArrayList<>();
                out.put(line.substring(4), current);
            } else if (current != null && !line.isEmpty()) {
                current.add(line);
            }
        }
        return out;
    }

    private static void record(Map<String, List<String>> actual) throws IOException {
        String dir = System.getenv("CASE_STUDY_RECORD_DIR");
        if (dir == null || dir.isBlank()) {
            return;
        }
        StringBuilder sb = new StringBuilder();
        actual.forEach((id, lines) -> {
            sb.append("=== ").append(id).append('\n');
            lines.forEach(l -> sb.append(l).append('\n'));
        });
        Path target = Path.of(dir, Path.of(GOLDEN).getFileName().toString());
        Files.createDirectories(target.getParent());
        Files.writeString(target, sb.toString(), StandardCharsets.UTF_8);
    }
}
