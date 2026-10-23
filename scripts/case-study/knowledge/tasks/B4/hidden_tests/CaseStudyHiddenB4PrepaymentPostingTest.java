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
package org.apache.fineract.portfolio.loanaccount.domain.transactionprocessor.impl;

import static org.mockito.Mockito.mock;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.stream.IntStream;
import java.util.stream.Stream;
import org.apache.fineract.infrastructure.businessdate.domain.BusinessDateType;
import org.apache.fineract.infrastructure.core.domain.ActionContext;
import org.apache.fineract.infrastructure.core.domain.ExternalId;
import org.apache.fineract.infrastructure.core.domain.FineractPlatformTenant;
import org.apache.fineract.infrastructure.core.service.ExternalIdFactory;
import org.apache.fineract.infrastructure.core.service.ThreadLocalContextUtil;
import org.apache.fineract.organisation.monetary.domain.MonetaryCurrency;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.domain.MoneyHelper;
import org.apache.fineract.organisation.office.domain.Office;
import org.apache.fineract.portfolio.loanaccount.domain.ChangedTransactionDetail;
import org.apache.fineract.portfolio.loanaccount.domain.Loan;
import org.apache.fineract.portfolio.loanaccount.domain.LoanRepaymentScheduleInstallment;
import org.apache.fineract.portfolio.loanaccount.domain.LoanTransaction;
import org.apache.fineract.portfolio.loanaccount.domain.transactionprocessor.AbstractLoanRepaymentScheduleTransactionProcessor;
import org.apache.fineract.portfolio.loanaccount.domain.transactionprocessor.MoneyHolder;
import org.apache.fineract.portfolio.loanaccount.domain.transactionprocessor.TransactionCtx;
import org.apache.fineract.portfolio.loanaccount.serialization.LoanChargeValidator;
import org.apache.fineract.portfolio.loanaccount.service.LoanBalanceService;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.mockito.MockedStatic;
import org.mockito.Mockito;

/**
 * Case study B4 (hidden acceptance suite): repayment posting under the "Due penalty, fee, interest, principal, In advance principal, penalty, fee, interest" strategy
 * ({@code due-penalty-fee-interest-principal-in-advance-principal-penalty-fee-interest-strategy}).
 *
 * <p>
 * Posting must conserve money: every repayment is split into principal, interest, fee and penalty portions plus any
 * overpayment, and those parts add up to exactly the amount received; across a loan, what the installments record as
 * paid equals the cash applied to them; no component is ever paid beyond what was charged. Each installment is paid in
 * the strategy's documented order. The suite replays generated loans and repayment sequences (in advance, on the due
 * date and late; partial, exact and overpaying) and checks every repayment against those rules and against an
 * independent allocation model of the strategy.
 */
class CaseStudyHiddenB4PrepaymentPostingTest {

    private static final long SEED = 20261025L;
    private static final int LOANS = 120;
    private static final MonetaryCurrency[] CURRENCIES = { new MonetaryCurrency("USD", 2, 1), new MonetaryCurrency("JPY", 0, 1),
            new MonetaryCurrency("KWD", 3, 1) };
    private static MockedStatic<MoneyHelper> moneyHelper;

    @BeforeAll
    static void init() {
        moneyHelper = Mockito.mockStatic(MoneyHelper.class);
        moneyHelper.when(MoneyHelper::getRoundingMode).thenReturn(RoundingMode.HALF_EVEN);
        moneyHelper.when(MoneyHelper::getMathContext).thenReturn(new MathContext(12, RoundingMode.HALF_EVEN));
    }

    @AfterAll
    static void tearDown() {
        moneyHelper.close();
    }

    @BeforeEach
    void context() {
        ThreadLocalContextUtil.setTenant(new FineractPlatformTenant(1L, "default", "Default", "Asia/Kolkata", null));
        ThreadLocalContextUtil.setActionContext(ActionContext.DEFAULT);
        ThreadLocalContextUtil.setBusinessDates(new HashMap<>(Map.of(BusinessDateType.BUSINESS_DATE, LocalDate.of(2024, 1, 1))));
    }

    @AfterEach
    void resetContext() {
        ThreadLocalContextUtil.reset();
    }

    private static AbstractLoanRepaymentScheduleTransactionProcessor processor() {
        return new DuePenFeeIntPriInAdvancePriPenFeeIntLoanRepaymentScheduleTransactionProcessor(mock(ExternalIdFactory.class), mock(LoanChargeValidator.class), mock(LoanBalanceService.class));
    }

    // --- named scenarios ------------------------------------------------------------------------------------------------

    @Test
    void anExactRepaymentOnTheDueDatePaysThatInstallmentOnly() {
        MonetaryCurrency usd = CURRENCIES[0];
        LoanCase loan = LoanCase.of(usd, LocalDate.of(2024, 1, 31), new long[][] { { 10_000, 1_250, 300, 0 }, { 10_000, 1_100, 300, 0 } });
        Posting posting = loan.pay(processor(), LocalDate.of(2024, 2, 29), 11_550);
        assertConserved(loan, posting);
        Assertions.assertEquals(List.of(10_000L, 1_250L, 300L, 0L, 0L), posting.portions(), "portions of the repayment");
        Assertions.assertEquals(List.of(0L, 0L, 0L, 0L), loan.paid(1), "the second installment must be untouched");
    }

    @Test
    void anEndOfDayBatchOfDueDateRepaymentsNeverPostsMoreThanItReceives() {
        MonetaryCurrency usd = CURRENCIES[0];
        LoanCase loan = LoanCase.of(usd, LocalDate.of(2024, 1, 31),
                new long[][] { { 8_333, 2_917, 0, 0 }, { 8_333, 2_764, 0, 0 }, { 8_334, 2_611, 0, 0 } });
        long received = 0;
        for (int i = 0; i < 3; i++) {
            Posting posting = loan.pay(processor(), loan.dueDate(i), 11_000);
            assertConserved(loan, posting);
            received += 11_000;
        }
        Assertions.assertEquals(received, loan.totalPaid(), "the schedule records more (or less) than the cash received");
    }

    @Test
    void anOverpaymentIsKeptAsOverpaymentNotPostedTwice() {
        MonetaryCurrency usd = CURRENCIES[0];
        LoanCase loan = LoanCase.of(usd, LocalDate.of(2024, 3, 31), new long[][] { { 5_000, 400, 100, 50 }, { 5_000, 300, 100, 0 } });
        Posting posting = loan.pay(processor(), LocalDate.of(2024, 5, 31), 12_000);
        assertConserved(loan, posting);
        Assertions.assertEquals(12_000 - 10_950, posting.overpayment(), "overpayment");
        Assertions.assertEquals(10_950, loan.totalPaid(), "everything owed is paid exactly once");
    }

    // --- generated loans ------------------------------------------------------------------------------------------------

    @TestFactory
    Stream<DynamicTest> generatedRepaymentsConserveMoneyAndFollowTheStrategy() {
        Random random = new Random(SEED);
        return IntStream.range(0, LOANS).mapToObj(i -> {
            long seed = random.nextLong();
            return DynamicTest.dynamicTest(String.format("loan-%03d", i), () -> replay(new Random(seed)));
        });
    }

    private void replay(Random r) {
        MonetaryCurrency currency = CURRENCIES[r.nextInt(CURRENCIES.length)];
        long unit = (long) Math.pow(10, currency.getDigitsAfterDecimal());
        int n = 1 + r.nextInt(8);
        long[][] rows = new long[n][];
        for (int i = 0; i < n; i++) {
            rows[i] = new long[] { unit * (50 + r.nextInt(2_000)) + r.nextInt((int) unit), r.nextInt(3) == 0 ? 0 : r.nextInt((int) (unit * 300)),
                    r.nextInt(3) == 0 ? r.nextInt((int) (unit * 40)) : 0, r.nextInt(5) == 0 ? r.nextInt((int) (unit * 25)) : 0 };
        }
        LocalDate start = LocalDate.of(2024, 1, 1).plusDays(r.nextInt(400));
        LoanCase loan = LoanCase.of(currency, start, rows);
        LocalDate date = start.plusDays(r.nextInt(10));
        int repayments = 1 + r.nextInt(6);
        for (int k = 0; k < repayments && loan.totalOutstanding() > 0; k++) {
            int target = Math.min(n - 1, r.nextInt(n));
            LocalDate candidate = switch (r.nextInt(3)) {
                case 0 -> loan.dueDate(target).minusDays(1 + r.nextInt(20));
                case 1 -> loan.dueDate(target);
                default -> loan.dueDate(target).plusDays(1 + r.nextInt(45));
            };
            date = candidate.isBefore(date) ? date : candidate;
            long amount = switch (r.nextInt(4)) {
                case 0 -> loan.installmentOutstanding(loan.firstUnpaid());
                case 1 -> loan.totalOutstanding() + r.nextInt((int) (unit * 50));
                default -> 1 + (long) (r.nextDouble() * loan.totalOutstanding());
            };
            if (amount <= 0) {
                continue;
            }
            long[][] expected = loan.model(date, amount);
            Posting posting = loan.pay(processor(), date, amount);
            assertConserved(loan, posting);
            for (int i = 0; i < n; i++) {
                Assertions.assertEquals(List.of(expected[i][0], expected[i][1], expected[i][2], expected[i][3]), loan.paid(i),
                        "installment " + (i + 1) + " after a repayment of " + amount + " on " + date + " (principal, interest, fee, penalty paid)");
            }
        }
    }

    // --- the rules --------------------------------------------------------------------------------------------------------

    private static void assertConserved(LoanCase loan, Posting posting) {
        List<Long> p = posting.portions();
        Assertions.assertEquals(posting.amount, p.get(0) + p.get(1) + p.get(2) + p.get(3) + p.get(4),
                "principal + interest + fee + penalty + overpayment must equal the repayment of " + posting.amount + " on " + posting.date
                        + ", got " + p);
        Assertions.assertEquals(loan.cashApplied, loan.totalPaid(),
                "the installments record " + loan.totalPaid() + " as paid but only " + loan.cashApplied + " was applied to them");
        for (int i = 0; i < loan.installments.size(); i++) {
            for (int c = 0; c < 4; c++) {
                Assertions.assertTrue(loan.paid(i).get(c) <= loan.charged[i][c],
                        "installment " + (i + 1) + " component " + c + " is paid beyond what was charged");
            }
        }
    }

    record Posting(LocalDate date, long amount, LoanTransaction transaction, MonetaryCurrency currency) {

        List<Long> portions() {
            return List.of(minor(transaction.getPrincipalPortion(currency)), minor(transaction.getInterestPortion(currency)),
                    minor(transaction.getFeeChargesPortion(currency)), minor(transaction.getPenaltyChargesPortion(currency)), overpayment());
        }

        long overpayment() {
            return minor(transaction.getOverPaymentPortion(currency));
        }

        private long minor(Money money) {
            return money == null ? 0 : money.getAmount().movePointRight(currency.getDigitsAfterDecimal()).longValueExact();
        }
    }

    static final class LoanCase {

        MonetaryCurrency currency;
        Loan loan = mock(Loan.class);
        Office office = mock(Office.class);
        List<LoanRepaymentScheduleInstallment> installments = new ArrayList<>();
        long[][] charged;
        long[][] modelPaid;
        long cashApplied;

        static LoanCase of(MonetaryCurrency currency, LocalDate start, long[][] rows) {
            LoanCase c = new LoanCase();
            c.currency = currency;
            c.charged = rows;
            c.modelPaid = new long[rows.length][4];
            LocalDate from = start;
            for (int i = 0; i < rows.length; i++) {
                LocalDate due = start.plusMonths(i + 1L);
                c.installments.add(new LoanRepaymentScheduleInstallment(c.loan, i + 1, from, due, c.major(rows[i][0]), c.major(rows[i][1]),
                        c.major(rows[i][2]), c.major(rows[i][3]), false, null, BigDecimal.ZERO));
                from = due;
            }
            return c;
        }

        BigDecimal major(long minor) {
            return BigDecimal.valueOf(minor, currency.getDigitsAfterDecimal());
        }

        long minor(Money money) {
            return money.getAmount().movePointRight(currency.getDigitsAfterDecimal()).longValueExact();
        }

        LocalDate dueDate(int i) {
            return installments.get(i).getDueDate();
        }

        Posting pay(AbstractLoanRepaymentScheduleTransactionProcessor processor, LocalDate date, long amount) {
            ThreadLocalContextUtil.setBusinessDates(new HashMap<>(Map.of(BusinessDateType.BUSINESS_DATE, date)));
            LoanTransaction tx = LoanTransaction.repayment(office, Money.of(currency, major(amount)), null, date, ExternalId.empty());
            processor.processLatestTransaction(tx, new TransactionCtx(currency, installments, Set.of(), new MoneyHolder(Money.zero(currency)),
                    new ChangedTransactionDetail(), List.of()));
            Posting posting = new Posting(date, amount, tx, currency);
            cashApplied += amount - posting.overpayment();
            return posting;
        }

        List<Long> paid(int i) {
            LoanRepaymentScheduleInstallment in = installments.get(i);
            return List.of(minor(in.getPrincipalCompleted(currency)), minor(in.getInterestPaid(currency)), minor(in.getFeeChargesPaid(currency)),
                    minor(in.getPenaltyChargesPaid(currency)));
        }

        long totalPaid() {
            long sum = 0;
            for (int i = 0; i < installments.size(); i++) {
                sum += paid(i).stream().mapToLong(Long::longValue).sum();
            }
            return sum;
        }

        long installmentOutstanding(int i) {
            long sum = 0;
            for (int c = 0; c < 4; c++) {
                sum += charged[i][c] - modelPaid[i][c];
            }
            return sum;
        }

        long totalOutstanding() {
            long sum = 0;
            for (int i = 0; i < charged.length; i++) {
                sum += installmentOutstanding(i);
            }
            return sum;
        }

        int firstUnpaid() {
            for (int i = 0; i < charged.length; i++) {
                if (installmentOutstanding(i) > 0) {
                    return i;
                }
            }
            return charged.length - 1;
        }

        /** The independent allocation model: advances the expected paid amounts by one repayment and returns them. */
        long[][] model(LocalDate date, long amount) {
            long remaining = amount;
            for (int i = 0; i < charged.length && remaining > 0; i++) {
                if (installmentOutstanding(i) == 0) {
                    continue;
                }
                for (int c : order(date, installments.get(i).getDueDate())) {
                    long pay = Math.min(remaining, charged[i][c] - modelPaid[i][c]);
                    modelPaid[i][c] += pay;
                    remaining -= pay;
                }
            }
            long[][] copy = new long[charged.length][];
            for (int i = 0; i < charged.length; i++) {
                copy[i] = modelPaid[i].clone();
            }
            return copy;
        }

        /** Component indexes: 0 principal, 1 interest, 2 fee, 3 penalty. */
        private static int[] order(LocalDate transactionDate, LocalDate dueDate) {
            // Due or late: penalty, fee, interest, principal. In advance (before the due date): principal, penalty, fee, interest.
            return transactionDate.isBefore(dueDate) ? new int[] { 0, 3, 2, 1 } : new int[] { 3, 2, 1, 0 };
        }
    }
}
