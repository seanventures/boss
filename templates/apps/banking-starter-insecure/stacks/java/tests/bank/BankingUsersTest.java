package bank;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bank.BankingHttp.Resp;
import io.dropwizard.testing.junit5.DropwizardAppExtension;
import io.dropwizard.testing.junit5.DropwizardExtensionsSupport;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

/** Users and the app's pages work. Functionality only: the planted flaws are not tested here. */
@ExtendWith(DropwizardExtensionsSupport.class)
class BankingUsersTest {

    static final DropwizardAppExtension<BankConfiguration> APP =
        new DropwizardAppExtension<>(BankApplication.class, "config.yml", BankingHttp.overrides());

    BankingHttp api;

    @BeforeEach
    void client() {
        api = new BankingHttp(APP.getLocalPort());
    }

    @Test
    void aUserCanRegisterAndLogIn() {
        String name = BankingHttp.uniqueName("u");
        Resp reg = api.register(name, BankingHttp.PASSWORD);
        assertEquals(201, reg.status());
        assertEquals(name, reg.body().get("username").asText());
        Resp login = api.login(name, BankingHttp.PASSWORD);
        assertEquals(200, login.status());
        assertEquals("Bearer", login.body().get("token_type").asText());
        assertEquals(200, api.get("/api/accounts", login.body().get("token").asText()).status());
    }

    @Test
    void aTakenUsernameCannotRegisterTwice() {
        String name = BankingHttp.uniqueName("u");
        assertEquals(201, api.register(name, BankingHttp.PASSWORD).status());
        assertEquals(409, api.register(name, BankingHttp.PASSWORD).status());
    }

    @Test
    void aWrongPasswordDoesNotLogIn() {
        String name = BankingHttp.uniqueName("u");
        api.register(name, BankingHttp.PASSWORD);
        Resp r = api.login(name, "Wrong-12345");
        assertNotEquals(200, r.status());
        assertFalse(r.body().has("token"));
    }

    @Test
    void aChangedPasswordIsTheOneThatLogsIn() {
        String name = BankingHttp.uniqueName("u");
        api.register(name, BankingHttp.PASSWORD);
        String token = api.token(name, BankingHttp.PASSWORD);
        Resp r = api.post("/api/password", token,
            Map.of("current_password", BankingHttp.PASSWORD, "new_password", "Pw-7654321"));
        assertEquals(204, r.status());
        assertEquals(200, api.login(name, "Pw-7654321").status());
        assertNotEquals(200, api.login(name, BankingHttp.PASSWORD).status());
    }

    @Test
    void logoutAnswersNoContent() {
        String token = api.newUser();
        assertEquals(204, api.post("/api/logout", token, null).status());
    }

    @Test
    void theLandingPageShowsTheTrainingBanner() {
        Resp page = api.get("/", null);
        assertEquals(200, page.status());
        assertTrue(page.body().asText().contains("Insecure by design &mdash; training only"), page.body().asText());
        for (Resp r : new Resp[] {page, api.get("/health", null), api.get("/api/accounts", api.newUser())}) {
            assertEquals("training only", r.header("X-Insecure-By-Design"));
        }
    }

    @Test
    void theOpenApiDocumentAndHealthAreServed() {
        Resp doc = api.get("/openapi.json", null);
        assertEquals(200, doc.status());
        assertEquals("3.0.3", doc.body().get("openapi").asText());
        assertTrue(doc.body().get("paths").has("/api/accounts/{id}/withdraw"));
        assertEquals("ok", api.get("/health", null).body().get("status").asText());
    }
}
