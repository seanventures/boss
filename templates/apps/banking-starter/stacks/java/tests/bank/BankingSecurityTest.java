package bank;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bank.BankingHttp.Resp;
import io.dropwizard.testing.junit5.DropwizardAppExtension;
import io.dropwizard.testing.junit5.DropwizardExtensionsSupport;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

/** Refusals: no token, someone else's account, bad passwords, injection, missing headers. */
@ExtendWith(DropwizardExtensionsSupport.class)
class BankingSecurityTest {

    static final DropwizardAppExtension<BankConfiguration> APP =
        new DropwizardAppExtension<>(BankApplication.class, "config.yml", BankingHttp.overrides());

    BankingHttp api;

    @BeforeEach
    void client() {
        api = new BankingHttp(APP.getLocalPort());
    }

    @Test
    void everyAccountRouteRefusesARequestWithoutAValidToken() {
        String owner = api.newUser();
        long id = api.openAccount(owner);
        Map<String, Object> amount = Map.of("amount_cents", 100);
        for (String token : new String[] {null, "not-a-token", "A".repeat(43)}) {
            assertEquals(401, api.post("/api/accounts", token, Map.of()).status());
            assertEquals(401, api.get("/api/accounts", token).status());
            assertEquals(401, api.get("/api/accounts/" + id + "/balance", token).status());
            assertEquals(401, api.get("/api/accounts/" + id + "/entries", token).status());
            assertEquals(401, api.post("/api/accounts/" + id + "/deposit", token, amount).status());
            assertEquals(401, api.post("/api/accounts/" + id + "/withdraw", token, amount).status());
            assertEquals(401, api.post("/api/transfers", token,
                Map.of("from_account_id", id, "to_account_id", id + 1, "amount_cents", 1)).status());
            assertEquals(401, api.post("/api/password", token,
                Map.of("current_password", "x", "new_password", "Pw-7654321")).status());
        }
        assertEquals(0, api.balance(owner, id));
    }

    @Test
    void anotherUsersAccountIsNotFoundOnEveryRoute() {
        String alice = api.newUser();
        String mallory = api.newUser();
        long aliceAcct = api.openAccount(alice);
        long malloryAcct = api.openAccount(mallory);
        api.deposit(alice, aliceAcct, 5_000);

        assertEquals(404, api.get("/api/accounts/" + aliceAcct + "/balance", mallory).status());
        assertEquals(404, api.get("/api/accounts/" + aliceAcct + "/entries", mallory).status());
        assertEquals(404, api.deposit(mallory, aliceAcct, 100).status());
        assertEquals(404, api.withdraw(mallory, aliceAcct, 100).status());
        // Mallory cannot pull from Alice's account into her own, nor push into Alice's.
        assertEquals(404, api.post("/api/transfers", mallory,
            Map.of("from_account_id", aliceAcct, "to_account_id", malloryAcct, "amount_cents", 100)).status());
        api.deposit(mallory, malloryAcct, 100);
        assertEquals(404, api.post("/api/transfers", mallory,
            Map.of("from_account_id", malloryAcct, "to_account_id", aliceAcct, "amount_cents", 50)).status());
        // Alice's account is untouched and absent from Mallory's list.
        assertEquals(5_000, api.balance(alice, aliceAcct));
        assertEquals(100, api.balance(mallory, malloryAcct));
        assertEquals(1, api.get("/api/accounts", mallory).body().get("accounts").size());
        // "Not yours" and "does not exist" are indistinguishable.
        Resp missing = api.get("/api/accounts/999999/balance", mallory);
        Resp notMine = api.get("/api/accounts/" + aliceAcct + "/balance", mallory);
        assertEquals(missing.status(), notMine.status());
        assertEquals(missing.body(), notMine.body());
    }

    @Test
    void changingThePasswordNeedsTheCurrentOne() {
        String name = BankingHttp.uniqueName("pw");
        api.register(name, BankingHttp.PASSWORD);
        String token = api.token(name, BankingHttp.PASSWORD);
        Resp wrong = api.post("/api/password", token,
            Map.of("current_password", "Wrong-12345", "new_password", "Pw-7654321"));
        assertEquals(403, wrong.status());
        Resp missing = api.post("/api/password", token, Map.of("new_password", "Pw-7654321"));
        assertTrue(missing.status() == 400 || missing.status() == 422);
        // The old password still works: nothing changed.
        assertEquals(200, api.login(name, BankingHttp.PASSWORD).status());
    }

    @Test
    void aPasswordChangeRevokesTheOtherSessionsOnly() {
        String name = BankingHttp.uniqueName("rv");
        api.register(name, BankingHttp.PASSWORD);
        String laptop = api.token(name, BankingHttp.PASSWORD);
        String phone = api.token(name, BankingHttp.PASSWORD);
        Resp ok = api.post("/api/password", laptop,
            Map.of("current_password", BankingHttp.PASSWORD, "new_password", "Pw-7654321"));
        assertEquals(204, ok.status());
        assertEquals(200, api.get("/api/accounts", laptop).status());
        assertEquals(401, api.get("/api/accounts", phone).status());
        assertEquals(401, api.login(name, BankingHttp.PASSWORD).status());
        assertEquals(200, api.login(name, "Pw-7654321").status());
    }

    @Test
    void logoutRevokesTheToken() {
        String token = api.newUser();
        assertEquals(200, api.get("/api/accounts", token).status());
        assertEquals(204, api.post("/api/logout", token, null).status());
        assertEquals(401, api.get("/api/accounts", token).status());
    }

    @Test
    void loginsAreRateLimitedPerUsername() {
        String name = BankingHttp.uniqueName("rl");
        api.register(name, BankingHttp.PASSWORD);
        for (int i = 0; i < 5; i++) {
            assertEquals(401, api.login(name, "Wrong-12345").status());
        }
        Resp blocked = api.login(name, BankingHttp.PASSWORD);
        assertEquals(429, blocked.status());
        assertNotNull(blocked.header("Retry-After"));
        assertFalse(blocked.body().has("token"));
        // Another user is not affected.
        String other = BankingHttp.uniqueName("rl");
        api.register(other, BankingHttp.PASSWORD);
        assertEquals(200, api.login(other, BankingHttp.PASSWORD).status());
    }

    @Test
    void loginErrorsDoNotRevealWhetherAUsernameExists() {
        String name = BankingHttp.uniqueName("en");
        api.register(name, BankingHttp.PASSWORD);
        Resp wrongPassword = api.login(name, "Wrong-12345");
        Resp noSuchUser = api.login(BankingHttp.uniqueName("ghost"), "Wrong-12345");
        assertEquals(401, wrongPassword.status());
        assertEquals(wrongPassword.status(), noSuchUser.status());
        assertEquals(wrongPassword.body(), noSuchUser.body());
    }

    @Test
    void sqlInjectionInLoginAndPathsGetsNothing() {
        String victim = BankingHttp.uniqueName("sq");
        api.register(victim, BankingHttp.PASSWORD);
        for (String attack : List.of("' OR 1=1--", victim + "'--", "' OR '1'='1")) {
            Resp r = api.login(attack, "' OR 1=1--");
            assertEquals(401, r.status(), attack);
            assertFalse(r.body().has("token"));
        }
        Resp reg = api.register("x' OR 1=1--", BankingHttp.PASSWORD);
        assertEquals(422, reg.status());
        String token = api.newUser();
        assertEquals(404, api.get("/api/accounts/1%20OR%201=1/balance", token).status());
        assertEquals(404, api.get("/api/accounts/1'--/balance", token).status());
        // The victim can still log in: nothing was altered.
        assertEquals(200, api.login(victim, BankingHttp.PASSWORD).status());
    }

    @Test
    void passwordsAreStoredAsArgon2idHashes() throws Exception {
        String name = BankingHttp.uniqueName("hs");
        api.register(name, BankingHttp.PASSWORD);
        String db = APP.getConfiguration().getDatabasePath();
        try (Connection c = DriverManager.getConnection("jdbc:sqlite:" + db);
             PreparedStatement s = c.prepareStatement("SELECT password_hash FROM users WHERE username = ?")) {
            s.setString(1, name);
            try (ResultSet rs = s.executeQuery()) {
                assertTrue(rs.next());
                String stored = rs.getString(1);
                assertTrue(stored.startsWith("$argon2id$v=19$"), "not argon2id");
                assertFalse(stored.contains(BankingHttp.PASSWORD));
            }
        }
    }

    @Test
    void securityHeadersArePresentAndNoCorsIsGranted() {
        String token = api.newUser();
        for (Resp r : List.of(api.get("/api/accounts", token), api.get("/health", null),
                              api.login("nobody-here", "Wrong-12345"))) {
            assertEquals("nosniff", r.header("X-Content-Type-Options"));
            assertEquals("DENY", r.header("X-Frame-Options"));
            assertTrue(r.header("Content-Security-Policy").contains("default-src 'none'"));
            assertEquals("no-store", r.header("Cache-Control"));
            assertEquals("no-referrer", r.header("Referrer-Policy"));
            assertEquals(null, r.header("Access-Control-Allow-Origin"));
        }
    }

    @Test
    void errorsDoNotLeakInternals() {
        String token = api.newUser();
        Resp r = api.post("/api/accounts", token, "{not json");
        assertEquals(400, r.status());
        String body = String.valueOf(r.body());
        assertFalse(body.contains("Exception") || body.contains("at bank.") || body.contains("jackson"), body);
    }
}
