package bank;

import bank.auth.Users;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Locale;

/**
 * Seeds the test users BOSS hands a preview in {@code BOSS_TEST_USERS}
 * ({@code [{"username": ..., "password": ..., "role": ...}]}), never when
 * {@code appEnv} is "production". Existing users are left alone; malformed entries are skipped.
 * Passwords are hashed like any other; nothing is logged but the count.
 */
public final class TestUsers {

    private TestUsers() {}

    public static int seed(String json, String appEnv, Users users, ObjectMapper mapper) {
        if (json == null || json.isBlank()
            || "production".equals(appEnv == null ? "" : appEnv.trim().toLowerCase(Locale.ROOT))) {
            return 0;
        }
        JsonNode list;
        try {
            list = mapper.readTree(json);
        } catch (Exception malformed) {
            return 0;
        }
        if (list == null || !list.isArray()) {
            return 0;
        }
        int seeded = 0;
        for (JsonNode u : list) {
            String username = u.path("username").asText("");
            String password = u.path("password").asText("");
            if (!username.matches("^[a-z0-9_.-]{3,32}$") || password.length() < 8
                || password.length() > 128 || users.exists(username)) {
                continue;
            }
            if (users.create(username, password).isPresent()) {
                seeded++;
            }
        }
        return seeded;
    }
}
