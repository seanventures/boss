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
package org.apache.fineract.portfolio.loanaccount.domain;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.withSettings;

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
import java.util.EnumMap;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Supplier;
import java.util.stream.Stream;
import org.apache.fineract.infrastructure.businessdate.domain.BusinessDateType;
import org.apache.fineract.infrastructure.core.domain.ExternalId;
import org.apache.fineract.infrastructure.core.service.ThreadLocalContextUtil;
import org.apache.fineract.organisation.monetary.domain.MonetaryCurrency;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.domain.MoneyHelper;
import org.apache.fineract.portfolio.charge.domain.Charge;
import org.apache.fineract.portfolio.charge.domain.ChargeCalculationType;
import org.apache.fineract.portfolio.charge.domain.ChargePaymentMode;
import org.apache.fineract.portfolio.charge.domain.ChargeTimeType;
import org.apache.fineract.portfolio.loanaccount.serialization.LoanChargeValidator;
import org.apache.fineract.portfolio.loanaccount.service.LoanBalanceService;
import org.apache.fineract.portfolio.loanaccount.service.LoanChargeService;
import org.apache.fineract.portfolio.loanaccount.service.LoanScheduleGeneratorService;
import org.apache.fineract.portfolio.loanaccount.service.LoanTransactionProcessingService;
import org.apache.fineract.portfolio.tax.service.ChargeTaxApplicationService;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.mockito.MockedStatic;
import org.mockito.Mockito;
import org.mockito.quality.Strictness;

/**
 * Case study B2 (hidden acceptance suite): characterisation of loan fee and penalty charge calculation.
 *
 * <p>
 * Generates a deterministic set of loans and charges across calculation types (flat, % of amount, % of amount and
 * interest, % of interest, % of disbursement), charge times (disbursement, specified due date, instalment fee,
 * overdue), minimum and maximum caps, currency digits and rounding modes, and compares the charge amounts, the base
 * amounts the percentages apply to, the per-installment fee split and the schedule's fee and penalty portions with
 * the values recorded on the pinned Fineract release.
 *
 * <p>
 * Uses only the public API the B2 brief keeps stable: {@link LoanChargeService} (its current public constructor and
 * public methods) and {@link SingleLoanChargeRepaymentScheduleProcessingWrapper#reprocess}.
 *
 * <p>
 * Record the golden file (on the pinned release only): {@code CASE_STUDY_RECORD_DIR=/some/dir ./gradlew
 * :fineract-loan:test --tests '*CaseStudyHiddenB2*'}.
 */
class CaseStudyHiddenB2ChargeCalculationTest {

    private static final String GOLDEN = "case-study/b2_charge_calculations.golden";
    private static final long SEED = 20261023L;
    private static final int CASES = 240;

    private static MockedStatic<MoneyHelper> moneyHelper;
    private static RoundingMode roundingMode = RoundingMode.HALF_EVEN;
    private static Map<String, List<String>> golden;

    private static final MonetaryCurrency[] CURRENCIES = { new MonetaryCurrency("USD", 2, 1), new MonetaryCurrency("JPY", 0, 1),
            new MonetaryCurrency("KWD", 3, 1), new MonetaryCurrency("EUR", 2, 5) };
    private static final RoundingMode[] ROUNDING = { RoundingMode.HALF_EVEN, RoundingMode.HALF_UP, RoundingMode.HALF_DOWN };
    private static final ChargeCalculationType[] CALCULATIONS = { ChargeCalculationType.FLAT, ChargeCalculationType.PERCENT_OF_AMOUNT,
            ChargeCalculationType.PERCENT_OF_AMOUNT_AND_INTEREST, ChargeCalculationType.PERCENT_OF_INTEREST,
            ChargeCalculationType.PERCENT_OF_DISBURSEMENT_AMOUNT };
    private static final ChargeTimeType[] TIMES = { ChargeTimeType.DISBURSEMENT, ChargeTimeType.SPECIFIED_DUE_DATE,
            ChargeTimeType.INSTALMENT_FEE, ChargeTimeType.OVERDUE_INSTALLMENT };
    private static final LocalDate[] DISBURSEMENT_DATES = { LocalDate.of(2024, 1, 1), LocalDate.of(2024, 1, 31), LocalDate.of(2024, 2, 29),
            LocalDate.of(2023, 12, 15), LocalDate.of(2025, 3, 31) };

    @BeforeAll
    static void init() throws IOException {
        moneyHelper = Mockito.mockStatic(MoneyHelper.class);
        moneyHelper.when(MoneyHelper::getRoundingMode).thenAnswer(i -> roundingMode);
        moneyHelper.when(MoneyHelper::getMathContext).thenAnswer(i -> new MathContext(12, roundingMode));
        golden = readGolden();
    }

    @AfterAll
    static void tearDown() {
        moneyHelper.close();
    }

    @AfterEach
    void resetContext() {
        ThreadLocalContextUtil.reset();
    }

    @TestFactory
    Stream<DynamicTest> chargeCalculationsMatchThePinnedRelease() throws IOException {
        Map<String, List<String>> actual = new LinkedHashMap<>();
        Random random = new Random(SEED);
        for (int i = 0; i < CASES; i++) {
            ChargeCase c = ChargeCase.generate(random, i);
            actual.put(c.id, capture(() -> scenario(c)));
        }
        record(actual);
        return actual.entrySet().stream().map(e -> DynamicTest.dynamicTest(e.getKey(), () -> {
            Assertions.assertNotNull(golden, "golden file " + GOLDEN + " is missing from the test classpath");
            List<String> expected = golden.get(e.getKey());
            Assertions.assertNotNull(expected, "no recorded charge calculation for " + e.getKey());
            Assertions.assertEquals(String.join("\n", expected), String.join("\n", e.getValue()),
                    "charge calculation differs from the pinned release for " + e.getKey());
        }));
    }

    // --- scenario ---------------------------------------------------------------------------------------------------

    private static List<String> scenario(ChargeCase c) {
        roundingMode = c.roundingMode;
        ThreadLocalContextUtil.setBusinessDates(new HashMap<>(new EnumMap<>(Map.of(BusinessDateType.BUSINESS_DATE, c.disbursementDate))));
        List<String> out = new ArrayList<>();
        out.add("case " + c.describe());

        LoanChargeService service = new LoanChargeService(mock(LoanChargeValidator.class), mock(LoanTransactionProcessingService.class),
                mock(LoanLifecycleStateMachine.class), mock(LoanBalanceService.class), mock(LoanScheduleGeneratorService.class),
                mock(ChargeTaxApplicationService.class));
        Loan loan = c.loan();

        // Base amounts and per-installment totals, straight from the service. Each step is recorded on its own, so a
        // step the release rejects (an exception is behaviour too) does not hide the others.
        LoanCharge probe = step(out, "created", () -> service.create(loan, c.chargeDefinition(), c.principal, c.amount, c.time,
                c.calculation, c.dueDate(), ChargePaymentMode.REGULAR, c.installments.size(), BigDecimal.ZERO, ExternalId.empty()),
                charge -> describe(charge, c.currency) + " split=" + split(charge, c.currency));
        if (probe != null) {
            step(out, "applied-to", () -> service.calculateAmountPercentageAppliedTo(loan, probe), CaseStudyHiddenB2ChargeCalculationTest::plain);
        }
        step(out, "per-installment-total", () -> service.calculatePerInstallmentChargeAmount(loan, c.calculation, c.amount),
                CaseStudyHiddenB2ChargeCalculationTest::plain);

        // The schedule's fee and penalty portions for a charge created without a loan (as the schedule generator does).
        LoanCharge scheduled = step(out, "unattached", () -> service.create(null, c.chargeDefinition(), c.principal, c.amount, c.time,
                c.calculation, c.dueDate(), ChargePaymentMode.REGULAR, c.installments.size(), BigDecimal.ZERO, ExternalId.empty()),
                charge -> describe(charge, c.currency));
        List<LoanRepaymentScheduleInstallment> fresh = c.freshInstallments(loan);
        if (scheduled != null && (c.time != ChargeTimeType.SPECIFIED_DUE_DATE || !c.dueDate().isAfter(fresh.getLast().getDueDate()))) {
            step(out, "schedule", () -> {
                new SingleLoanChargeRepaymentScheduleProcessingWrapper().reprocess(c.currency, c.disbursementDate, fresh, scheduled);
                List<String> rows = new ArrayList<>();
                for (LoanRepaymentScheduleInstallment installment : fresh) {
                    rows.add("#" + installment.getInstallmentNumber() + " " + installment.getFromDate() + ".." + installment.getDueDate()
                            + " fee=" + amount(installment.getFeeChargesCharged(c.currency)) + " penalty="
                            + amount(installment.getPenaltyChargesCharged(c.currency)));
                }
                return rows;
            }, rows -> "\n  " + String.join("\n  ", rows));
        }

        // Changing the amount recalculates it. (Fineract cannot update a % of disbursement charge that has no tranche: it
        // fails with a NullPointerException, a latent defect this suite neither requires nor forbids.)
        if (probe != null && c.calculation != ChargeCalculationType.PERCENT_OF_DISBURSEMENT_AMOUNT) {
            step(out, "updated", () -> {
                service.update(probe, c.newAmount, c.dueDate(), c.installments.size());
                return probe;
            }, charge -> describe(charge, c.currency) + " split=" + split(charge, c.currency));
        }
        return out;
    }

    private static <T> T step(List<String> out, String label, Supplier<T> action, java.util.function.Function<T, String> render) {
        try {
            T value = action.get();
            for (String line : (label + " " + render.apply(value)).split("\n", -1)) {
                out.add(line);
            }
            return value;
        } catch (RuntimeException e) {
            out.add(label + " ERROR " + e.getClass().getName());
            return null;
        }
    }

    private static String describe(LoanCharge charge, MonetaryCurrency currency) {
        return "amount=" + plain(charge.getAmount()) + " outstanding=" + plain(charge.getAmountOutstanding()) + " percentage="
                + plain(charge.getPercentage()) + " appliedTo=" + plain(charge.getAmountPercentageAppliedTo()) + " amountOrPercentage="
                + plain(charge.getAmountOrPercentage()) + " penalty=" + charge.isPenaltyCharge();
    }

    private static String split(LoanCharge charge, MonetaryCurrency currency) {
        List<String> parts = new ArrayList<>();
        charge.getLoanInstallmentCharge().stream()
                .sorted((a, b) -> Integer.compare(a.getInstallment().getInstallmentNumber(), b.getInstallment().getInstallmentNumber()))
                .forEach(ic -> parts.add(ic.getInstallment().getInstallmentNumber() + ":" + plain(ic.getAmount(currency).getAmount())));
        return String.join(",", parts);
    }

    // --- generated cases --------------------------------------------------------------------------------------------

    static final class ChargeCase {

        String id;
        MonetaryCurrency currency;
        RoundingMode roundingMode;
        LocalDate disbursementDate;
        BigDecimal principal;
        BigDecimal totalInterest;
        ChargeCalculationType calculation;
        ChargeTimeType time;
        boolean penalty;
        BigDecimal amount;
        BigDecimal newAmount;
        BigDecimal minCap;
        BigDecimal maxCap;
        int dueInstallment;
        int dueOffsetDays;
        List<BigDecimal[]> installments = new ArrayList<>();

        static ChargeCase generate(Random r, int index) {
            ChargeCase c = new ChargeCase();
            c.id = String.format("charge-%03d", index);
            c.currency = CURRENCIES[r.nextInt(CURRENCIES.length)];
            c.roundingMode = ROUNDING[r.nextInt(ROUNDING.length)];
            c.disbursementDate = DISBURSEMENT_DATES[r.nextInt(DISBURSEMENT_DATES.length)];
            c.calculation = CALCULATIONS[r.nextInt(CALCULATIONS.length)];
            c.time = TIMES[r.nextInt(TIMES.length)];
            if (c.time == ChargeTimeType.OVERDUE_INSTALLMENT && c.calculation == ChargeCalculationType.PERCENT_OF_DISBURSEMENT_AMOUNT) {
                c.calculation = ChargeCalculationType.PERCENT_OF_AMOUNT;
            }
            c.penalty = c.time == ChargeTimeType.OVERDUE_INSTALLMENT || (c.time == ChargeTimeType.SPECIFIED_DUE_DATE && r.nextInt(3) == 0);
            int n = 1 + r.nextInt(12);
            BigDecimal principalSum = BigDecimal.ZERO;
            BigDecimal interestSum = BigDecimal.ZERO;
            for (int i = 0; i < n; i++) {
                BigDecimal p = BigDecimal.valueOf(1_000 + r.nextInt(2_000_000), 3).setScale(c.currency.getDigitsAfterDecimal(),
                        RoundingMode.HALF_EVEN);
                BigDecimal in = BigDecimal.valueOf(r.nextInt(400_000), 3).setScale(c.currency.getDigitsAfterDecimal(), RoundingMode.HALF_EVEN);
                c.installments.add(new BigDecimal[] { p, in });
                principalSum = principalSum.add(p);
                interestSum = interestSum.add(in);
            }
            c.principal = principalSum;
            c.totalInterest = interestSum;
            c.amount = c.calculation == ChargeCalculationType.FLAT ? BigDecimal.valueOf(1 + r.nextInt(250_000), 3)
                    : BigDecimal.valueOf(1 + r.nextInt(12_500), 4);
            c.newAmount = c.amount.multiply(BigDecimal.valueOf(3)).divide(BigDecimal.valueOf(2), 6, RoundingMode.HALF_EVEN);
            if (c.calculation != ChargeCalculationType.FLAT && r.nextInt(3) == 0) {
                c.minCap = BigDecimal.valueOf(r.nextInt(5_000), 2);
            }
            if (c.calculation != ChargeCalculationType.FLAT && r.nextInt(3) == 0) {
                c.maxCap = BigDecimal.valueOf(1_000 + r.nextInt(50_000), 2);
            }
            c.dueInstallment = r.nextInt(n);
            c.dueOffsetDays = r.nextInt(3) == 0 ? 0 : r.nextInt(40) - 10;
            return c;
        }

        String describe() {
            return id + " " + currency.getCode() + "/" + currency.getDigitsAfterDecimal() + "/" + currency.getInMultiplesOf() + " "
                    + roundingMode + " disb=" + disbursementDate + " principal=" + principal.toPlainString() + " interest="
                    + totalInterest.toPlainString() + " " + calculation + " " + time + " penalty=" + penalty + " amount="
                    + amount.toPlainString() + " new=" + newAmount.toPlainString() + " min=" + plain(minCap) + " max=" + plain(maxCap)
                    + " n=" + installments.size() + " due=" + dueDate();
        }

        LocalDate dueDate() {
            if (time != ChargeTimeType.SPECIFIED_DUE_DATE && time != ChargeTimeType.OVERDUE_INSTALLMENT) {
                return null;
            }
            return disbursementDate.plusMonths(dueInstallment + 1L).plusDays(dueOffsetDays);
        }

        Charge chargeDefinition() {
            Charge charge = mock(Charge.class, withSettings().strictness(Strictness.LENIENT));
            Integer timeValue = time.getValue();
            Integer calculationValue = calculation.getValue();
            Integer modeValue = ChargePaymentMode.REGULAR.getValue();
            when(charge.getId()).thenReturn(1L);
            when(charge.getName()).thenReturn("case-study charge");
            when(charge.getCurrencyCode()).thenReturn(currency.getCode());
            when(charge.isPenalty()).thenReturn(penalty);
            when(charge.getMinCap()).thenReturn(minCap);
            when(charge.getMaxCap()).thenReturn(maxCap);
            when(charge.getChargeTimeType()).thenReturn(timeValue);
            when(charge.getChargeCalculation()).thenReturn(calculationValue);
            when(charge.getAmount()).thenReturn(amount);
            when(charge.getChargePaymentMode()).thenReturn(modeValue);
            return charge;
        }

        Loan loan() {
            Loan loan = mock(Loan.class, withSettings().strictness(Strictness.LENIENT));
            // Values are built before stubbing: building Money inside when(...) would interleave stubbings.
            Money principalMoney = Money.of(currency, principal);
            List<LoanRepaymentScheduleInstallment> schedule = freshInstallments(loan);
            when(loan.getCurrency()).thenReturn(currency);
            when(loan.getPrincipal()).thenReturn(principalMoney);
            when(loan.getTotalInterest()).thenReturn(totalInterest);
            when(loan.isMultiDisburmentLoan()).thenReturn(false);
            when(loan.isInterestBearing()).thenReturn(totalInterest.signum() > 0);
            when(loan.getDisbursementDate()).thenReturn(disbursementDate);
            when(loan.fetchNumberOfInstallmentsAfterExceptions()).thenReturn(installments.size());
            when(loan.getLoanRepaymentScheduleInstallmentsSize()).thenReturn(installments.size());
            when(loan.getRepaymentScheduleInstallments()).thenReturn(schedule);
            return loan;
        }

        List<LoanRepaymentScheduleInstallment> freshInstallments(Loan loan) {
            List<LoanRepaymentScheduleInstallment> list = new ArrayList<>();
            LocalDate from = disbursementDate;
            for (int i = 0; i < installments.size(); i++) {
                LocalDate due = disbursementDate.plusMonths(i + 1L);
                list.add(new LoanRepaymentScheduleInstallment(loan, i + 1, from, due, installments.get(i)[0], installments.get(i)[1],
                        BigDecimal.ZERO, BigDecimal.ZERO, false, null, BigDecimal.ZERO));
                from = due;
            }
            return list;
        }
    }

    // --- helpers ----------------------------------------------------------------------------------------------------

    private static List<String> capture(Supplier<List<String>> scenario) {
        try {
            return scenario.get();
        } catch (RuntimeException e) {
            return List.of("ERROR " + e.getClass().getName());
        }
    }

    private static String amount(Money money) {
        return money == null ? "null" : plain(money.getAmount());
    }

    private static String plain(BigDecimal value) {
        return value == null ? "null" : value.stripTrailingZeros().toPlainString();
    }

    private static Map<String, List<String>> readGolden() throws IOException {
        try (InputStream in = CaseStudyHiddenB2ChargeCalculationTest.class.getClassLoader().getResourceAsStream(GOLDEN)) {
            if (in == null) {
                return null;
            }
            Map<String, List<String>> out = new LinkedHashMap<>();
            List<String> current = null;
            for (String line : new String(in.readAllBytes(), StandardCharsets.UTF_8).split("\n", -1)) {
                if (line.startsWith("=== ")) {
                    current = new ArrayList<>();
                    out.put(line.substring(4), current);
                } else if (current != null && !line.isEmpty()) {
                    current.add(line);
                }
            }
            return out;
        }
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
