package bank;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bank.auth.Users;
import bank.db.Database;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

/** BOSS_TEST_USERS seeding: on outside production, off in production. */
class BankingTestUsersTest {

    private static final String USERS = "[{\"username\":\"tester1\",\"password\":\"Pw-1234567\",\"role\":\"user\"},"
        + "{\"username\":\"BAD NAME\",\"password\":\"Pw-1234567\"},{\"username\":\"tester2\"}]";

    private static Users users() throws Exception {
        Path db = Files.createTempDirectory("bank-seed").resolve("bank.db");
        return new Users(Database.open(db.toString()));
    }

    @Test
    void testUsersAreSeededOutsideProductionAndSkippedInIt() throws Exception {
        Users dev = users();
        assertEquals(1, TestUsers.seed(USERS, "development", dev, new ObjectMapper()));
        assertEquals(Users.Check.OK, dev.check("tester1", "Pw-1234567").check());
        assertFalse(dev.exists("tester2"));
        assertEquals(0, TestUsers.seed(USERS, "development", dev, new ObjectMapper()));

        Users prod = users();
        assertEquals(0, TestUsers.seed(USERS, "production", prod, new ObjectMapper()));
        assertFalse(prod.exists("tester1"));
        assertTrue(TestUsers.seed("not json", "development", prod, new ObjectMapper()) == 0);
    }
}
