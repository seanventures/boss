package bank;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bank.BankingHttp.Resp;
import com.fasterxml.jackson.databind.JsonNode;
import io.dropwizard.testing.junit5.DropwizardAppExtension;
import io.dropwizard.testing.junit5.DropwizardExtensionsSupport;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

/** The bank works: accounts, deposits, withdrawals, the ledger balance, transfers. */
@ExtendWith(DropwizardExtensionsSupport.class)
class BankingFlowTest {

    static final DropwizardAppExtension<BankConfiguration> APP =
        new DropwizardAppExtension<>(BankApplication.class, "config.yml", BankingHttp.overrides());

    BankingHttp api;

    @BeforeEach
    void client() {
        api = new BankingHttp(APP.getLocalPort());
    }

    @Test
    void aNewAccountStartsAtZeroAndIsListed() {
        String token = api.newUser();
        long id = api.openAccount(token);
        assertEquals(0, api.balance(token, id));
        Resp list = api.get("/api/accounts", token);
        assertEquals(200, list.status());
        assertEquals(1, list.body().get("accounts").size());
        assertEquals(id, list.body().get("accounts").get(0).get("id").asLong());
    }

    @Test
    void depositThenWithdrawMovesTheBalance() {
        String token = api.newUser();
        long id = api.openAccount(token);
        Resp d = api.deposit(token, id, 10_000);
        assertEquals(201, d.status());
        assertEquals(10_000, d.body().get("balance_cents").asLong());
        Resp w = api.withdraw(token, id, 2_550);
        assertEquals(201, w.status());
        assertEquals(7_450, api.balance(token, id));
    }

    @Test
    void theBalanceIsTheSumOfTheLedger() {
        String token = api.newUser();
        long id = api.openAccount(token);
        api.deposit(token, id, 500);
        api.deposit(token, id, 1_250);
        api.withdraw(token, id, 300);
        api.deposit(token, id, 1);
        JsonNode entries = api.get("/api/accounts/" + id + "/entries", token).body().get("entries");
        assertEquals(4, entries.size());
        long sum = 0;
        for (JsonNode e : entries) {
            long amount = e.get("amount_cents").asLong();
            sum += e.get("kind").asText().equals("deposit") ? amount : -amount;
        }
        assertEquals(1_451, sum);
        assertEquals(sum, api.balance(token, id));
    }

    @Test
    void aWithdrawalLargerThanTheBalanceIsRefused() {
        String token = api.newUser();
        long id = api.openAccount(token);
        api.deposit(token, id, 1_000);
        Resp r = api.withdraw(token, id, 1_001);
        assertEquals(409, r.status());
        assertEquals(1_000, api.balance(token, id));
    }

    @Test
    void replayingAnIdempotencyKeyDoesNotPostTwice() {
        String token = api.newUser();
        long id = api.openAccount(token);
        Map<String, Object> body = Map.of("amount_cents", 700, "idempotency_key", "dep-001");
        Resp first = api.post("/api/accounts/" + id + "/deposit", token, body);
        Resp again = api.post("/api/accounts/" + id + "/deposit", token, body);
        assertEquals(201, first.status());
        assertEquals(200, again.status());
        assertTrue(again.body().get("replayed").asBoolean());
        assertEquals(700, api.balance(token, id));
    }

    @Test
    void aTransferMovesMoneyBetweenOwnAccounts() {
        String token = api.newUser();
        long a = api.openAccount(token);
        long b = api.openAccount(token);
        api.deposit(token, a, 5_000);
        Resp t = api.post("/api/transfers", token,
            Map.of("from_account_id", a, "to_account_id", b, "amount_cents", 1_200));
        assertEquals(201, t.status());
        assertEquals(3_800, api.balance(token, a));
        assertEquals(1_200, api.balance(token, b));
        Resp tooMuch = api.post("/api/transfers", token,
            Map.of("from_account_id", a, "to_account_id", b, "amount_cents", 3_801));
        assertEquals(409, tooMuch.status());
    }

    @Test
    void concurrentWithdrawalsLeaveAConsistentBalance() throws Exception {
        String token = api.newUser();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            long id = api.openAccount(token);
            api.deposit(token, id, 100);
            CountDownLatch go = new CountDownLatch(1);
            List<Future<Integer>> results = new ArrayList<>();
            for (int i = 0; i < 2; i++) {
                results.add(pool.submit(() -> {
                    go.await();
                    return api.withdraw(token, id, 70).status();
                }));
            }
            go.countDown();
            List<Integer> statuses = new ArrayList<>();
            for (Future<Integer> f : results) {
                statuses.add(f.get());
            }
            assertTrue(statuses.contains(201) && statuses.contains(409), statuses.toString());
            assertEquals(30, api.balance(token, id));
        } finally {
            pool.shutdownNow();
        }
    }
}
