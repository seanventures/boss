package bank;

import static org.junit.jupiter.api.Assertions.assertEquals;

import io.dropwizard.testing.ConfigOverride;
import io.dropwizard.testing.junit5.DropwizardAppExtension;
import io.dropwizard.testing.junit5.DropwizardExtensionsSupport;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

/** Bearer tokens expire: an app whose tokens live one second refuses a two-second-old token. */
@ExtendWith(DropwizardExtensionsSupport.class)
class BankingTokenExpiryTest {

    static final DropwizardAppExtension<BankConfiguration> APP = new DropwizardAppExtension<>(
        BankApplication.class, "config.yml",
        BankingHttp.overrides(ConfigOverride.config("tokenTtlSeconds", "1")));

    @Test
    void anExpiredTokenIsRefused() throws Exception {
        BankingHttp api = new BankingHttp(APP.getLocalPort());
        String token = api.newUser();
        assertEquals(200, api.get("/api/accounts", token).status());
        Thread.sleep(2_100);
        assertEquals(401, api.get("/api/accounts", token).status());
    }
}
