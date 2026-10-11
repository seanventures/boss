package bank;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.dropwizard.testing.ConfigOverride;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A small HTTP client for the template's tests. The tests speak only HTTP to the routes the
 * manifest declares, so they keep working when the starter is adapted into another app.
 */
final class BankingHttp {

    static final ObjectMapper JSON = new ObjectMapper();
    private static final AtomicInteger SEQ = new AtomicInteger();
    static final String PASSWORD = "Pw-1234567";

    record Resp(int status, JsonNode body, HttpHeaders headers) {
        String header(String name) {
            return headers.firstValue(name).orElse(null);
        }
    }

    private final HttpClient http = HttpClient.newHttpClient();
    private final String base;

    BankingHttp(int port) {
        this.base = "http://127.0.0.1:" + port;
    }

    /** Config overrides for a test app: a free port and a fresh database file. */
    static ConfigOverride[] overrides(ConfigOverride... extra) {
        String db;
        try {
            Path dir = Files.createTempDirectory("bank-test");
            dir.toFile().deleteOnExit();
            db = dir.resolve("bank.db").toString();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
        ConfigOverride[] base = {
            ConfigOverride.config("server.applicationConnectors[0].port", "0"),
            ConfigOverride.config("databasePath", db),
            ConfigOverride.config("logging.level", "WARN"),
        };
        ConfigOverride[] all = new ConfigOverride[base.length + extra.length];
        System.arraycopy(base, 0, all, 0, base.length);
        System.arraycopy(extra, 0, all, base.length, extra.length);
        return all;
    }

    static String uniqueName(String prefix) {
        return (prefix + SEQ.incrementAndGet() + "_" + Long.toString(System.nanoTime() % 100000, 36))
            .toLowerCase();
    }

    Resp send(String method, String path, String token, Object body) {
        try {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
            if (token != null) {
                b.header("Authorization", "Bearer " + token);
            }
            String payload = body == null ? null
                : body instanceof String s ? s : JSON.writeValueAsString(body);
            if (payload != null) {
                b.header("Content-Type", "application/json");
                b.method(method, HttpRequest.BodyPublishers.ofString(payload));
            } else {
                b.method(method, HttpRequest.BodyPublishers.noBody());
            }
            HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
            JsonNode json = null;
            if (r.body() != null && !r.body().isBlank()) {
                try {
                    json = JSON.readTree(r.body());
                } catch (IOException notJson) {
                    json = JSON.getNodeFactory().textNode(r.body());
                }
            }
            return new Resp(r.statusCode(), json, r.headers());
        } catch (IOException e) {
            throw new IllegalStateException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(e);
        }
    }

    Resp get(String path, String token) {
        return send("GET", path, token, null);
    }

    Resp post(String path, String token, Object body) {
        return send("POST", path, token, body == null ? "{}" : body);
    }

    Resp register(String username, String password) {
        return post("/api/register", null, Map.of("username", username, "password", password));
    }

    Resp login(String username, String password) {
        return post("/api/login", null, Map.of("username", username, "password", password));
    }

    /** Registers a fresh user and returns a bearer token for it. */
    String newUser() {
        String name = uniqueName("user");
        Resp reg = register(name, PASSWORD);
        if (reg.status() != 201) {
            throw new AssertionError("register: " + reg.status() + " " + reg.body());
        }
        return token(name, PASSWORD);
    }

    String token(String username, String password) {
        Resp r = login(username, password);
        if (r.status() != 200) {
            throw new AssertionError("login: " + r.status() + " " + r.body());
        }
        return r.body().get("token").asText();
    }

    long openAccount(String token) {
        Resp r = post("/api/accounts", token, Map.of("currency", "USD"));
        if (r.status() != 201) {
            throw new AssertionError("open account: " + r.status() + " " + r.body());
        }
        return r.body().get("id").asLong();
    }

    Resp deposit(String token, long account, Object amount) {
        return post("/api/accounts/" + account + "/deposit", token, Map.of("amount_cents", amount));
    }

    Resp withdraw(String token, long account, Object amount) {
        return post("/api/accounts/" + account + "/withdraw", token, Map.of("amount_cents", amount));
    }

    long balance(String token, long account) {
        Resp r = get("/api/accounts/" + account + "/balance", token);
        if (r.status() != 200) {
            throw new AssertionError("balance: " + r.status() + " " + r.body());
        }
        return r.body().get("balance_cents").asLong();
    }
}
