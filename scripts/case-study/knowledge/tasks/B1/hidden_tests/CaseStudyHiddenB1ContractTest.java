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

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

/**
 * Case study B1 (hidden acceptance suite): the structural contract from the brief, checked by reflection only so that
 * the pinned release fails on assertions, never on compilation.
 */
class CaseStudyHiddenB1ContractTest {

    static final String SERVICE = "org.apache.fineract.portfolio.loanproduct.calc.InterestCalculationService";

    /** The day-count and rate-factor rules that must leave ProgressiveEMICalculator. */
    static final Set<String> MOVED = Set.of("getNumberOfDays", "isPeriodContainsFeb29", "numberOfDaysFeb29PeriodOnly",
            "calculatePeriodFractions", "calculatePeriodRatio", "calculateSeedDate", "calculateRateFactorPerPeriod",
            "calculateRateFactorPerPeriodForInterest", "calculateRateFactorPerPeriodBasedOnRepaymentFrequency",
            "rateFactorByRepaymentEveryDay", "rateFactorByRepaymentEveryWeek", "rateFactorByRepaymentEveryMonth",
            "rateFactorByRepaymentPeriod", "rateFactorByRepaymentPartialPeriod");

    private static Class<?> service() {
        try {
            return Class.forName(SERVICE);
        } catch (ClassNotFoundException e) {
            return Assertions.fail("the interest calculation service " + SERVICE + " does not exist");
        }
    }

    @Test
    void theInterestCalculationServiceExists() {
        Class<?> service = service();
        Assertions.assertTrue(Modifier.isPublic(service.getModifiers()), SERVICE + " must be public");
    }

    @Test
    void theServiceOwnsTheRateFactorRules() {
        List<String> names = Arrays.stream(service().getDeclaredMethods()).filter(m -> !m.isSynthetic()).map(Method::getName).toList();
        Assertions.assertTrue(names.stream().anyMatch(n -> n.toLowerCase(Locale.ROOT).contains("ratefactor")),
                "the service declares no rate-factor method: " + names);
        Assertions.assertTrue(names.stream().anyMatch(n -> n.toLowerCase(Locale.ROOT).contains("days")),
                "the service declares no day-count method: " + names);
    }

    @Test
    void theEmiCalculatorDelegatesToTheService() {
        Class<?> service = service();
        boolean holds = Arrays.stream(ProgressiveEMICalculator.class.getDeclaredFields())
                .filter(f -> !Modifier.isStatic(f.getModifiers())).map(Field::getType).anyMatch(service::isAssignableFrom);
        Assertions.assertTrue(holds, "ProgressiveEMICalculator holds no instance field of type " + SERVICE);
    }

    @Test
    void theRulesNoLongerLiveInTheEmiCalculator() {
        List<String> left = Arrays.stream(ProgressiveEMICalculator.class.getDeclaredMethods()).map(Method::getName).filter(MOVED::contains)
                .distinct().sorted().toList();
        Assertions.assertEquals(List.of(), left, "day-count / rate-factor rules still declared on ProgressiveEMICalculator");
    }
}
