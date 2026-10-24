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

import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.apache.fineract.portfolio.loanaccount.service.LoanChargeService;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;

/**
 * Case study B2 (hidden acceptance suite): the structural contract from the brief, checked by reflection only so that
 * the pinned release fails on assertions, never on compilation.
 */
class CaseStudyHiddenB2ContractTest {

    static final String SERVICE = "org.apache.fineract.portfolio.loanaccount.service.ChargeCalculationService";

    private static Class<?> service() {
        try {
            return Class.forName(SERVICE);
        } catch (ClassNotFoundException e) {
            return Assertions.fail("the charge calculation service " + SERVICE + " does not exist");
        }
    }

    private static List<String> declared(Class<?> type, Set<String> names) {
        return Arrays.stream(type.getDeclaredMethods()).map(Method::getName).filter(names::contains).distinct().sorted().toList();
    }

    @Test
    void theChargeCalculationServiceExists() {
        Assertions.assertTrue(Modifier.isPublic(service().getModifiers()), SERVICE + " must be public");
    }

    @Test
    void theServiceOwnsBaseAmountPercentageAndCapRules() {
        List<String> names = Arrays.stream(service().getDeclaredMethods()).filter(m -> !m.isSynthetic())
                .map(m -> m.getName().toLowerCase(Locale.ROOT)).toList();
        Assertions.assertTrue(names.stream().anyMatch(n -> n.contains("base") || n.contains("percent")),
                "the service declares no base-amount / percentage method: " + names);
        Assertions.assertTrue(names.stream().anyMatch(n -> n.contains("cap")), "the service declares no min/max cap method: " + names);
        Assertions.assertTrue(names.stream().anyMatch(n -> n.contains("installment") || n.contains("instalment")),
                "the service declares no per-installment fee method: " + names);
    }

    @Test
    void theScheduleWrapperNoLongerCalculatesChargeAmounts() {
        Assertions.assertEquals(List.of(),
                declared(SingleLoanChargeRepaymentScheduleProcessingWrapper.class, Set.of("getBaseAmount", "getInstallmentFee")),
                "charge amount rules still declared on SingleLoanChargeRepaymentScheduleProcessingWrapper");
    }

    @Test
    void theChargeServiceNoLongerCalculatesChargeAmounts() {
        Assertions.assertEquals(List.of(), declared(LoanChargeService.class, Set.of("calculateInstallmentChargeAmount", "getDerivedAmountForCharge")),
                "charge amount rules still declared on LoanChargeService");
    }
}
