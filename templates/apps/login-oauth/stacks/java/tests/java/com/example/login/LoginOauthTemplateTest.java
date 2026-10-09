package com.example.login;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/**
 * The login-oauth template's own tests, against its HTTP routes (not internals), with an
 * in-process fake identity provider: no network. Run: mvn -q test -Dtest=LoginOauthTemplateTest
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class LoginOauthTemplateTest {
    static final String BASE_URL = "https://app.example.test"; // https: the session cookie must be Secure
    static final ObjectMapper JSON = new ObjectMapper();
    static final HttpClient HTTP = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).build();
    static final LoginFakeIdp IDP;

    static {
        try {
            IDP = new LoginFakeIdp();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
        IDP.clients.put("fake-google-client", "fake-google-secret");
        IDP.clients.put("fake-github-client", "fake-github-secret");
        IDP.clients.put("fake-boss-client", "fake-boss-secret");
    }

    @DynamicPropertySource
    static void env(DynamicPropertyRegistry r) {
        r.add("APP_BASE_URL", () -> BASE_URL);
        r.add("AUTH_ADMIN_EMAILS", () -> "boss@example.com");
        r.add("GOOGLE_ISSUER", () -> IDP.issuer);
        r.add("GOOGLE_CLIENT_ID", () -> "fake-google-client");
        r.add("GOOGLE_CLIENT_SECRET", () -> "fake-google-secret");
        r.add("GITHUB_URL", () -> IDP.issuer);
        r.add("GITHUB_API_URL", () -> IDP.issuer + "/api");
        r.add("GITHUB_CLIENT_ID", () -> "fake-github-client");
        r.add("GITHUB_CLIENT_SECRET", () -> "fake-github-secret");
        r.add("OIDC_BOSS_ISSUER", () -> IDP.issuer);
        r.add("OIDC_BOSS_CLIENT_ID", () -> "fake-boss-client");
        r.add("OIDC_BOSS_CLIENT_SECRET", () -> "fake-boss-secret");
    }

    @LocalServerPort
    int port;

    @BeforeEach
    void reset() {
        IDP.tamperNonce = null;
        IDP.user = LoginFakeIdp.person("g-1", "ada@example.com", true, "Ada");
    }

    @AfterAll
    static void stop() {
        IDP.close();
    }

    /** A browser: its own cookie jar and manual redirects. */
    class Browser {
        final Map<String, String> jar = new LinkedHashMap<>();
        final List<String> setCookies = new ArrayList<>();

        HttpResponse<String> request(String method, String path, Map<String, String> headers) throws Exception {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                    .method(method, HttpRequest.BodyPublishers.noBody());
            if (!jar.isEmpty()) {
                b.header("Cookie", jar.entrySet().stream().map(e -> e.getKey() + "=" + e.getValue()).collect(Collectors.joining("; ")));
            }
            headers.forEach(b::header);
            HttpResponse<String> res = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
            for (String raw : res.headers().allValues("set-cookie")) {
                setCookies.add(raw);
                String pair = raw.split(";", 2)[0];
                String name = pair.substring(0, pair.indexOf('=')).trim();
                String value = pair.substring(pair.indexOf('=') + 1);
                if (value.isEmpty() || raw.toLowerCase().contains("max-age=0")) jar.remove(name); else jar.put(name, value);
            }
            return res;
        }

        HttpResponse<String> get(String path) throws Exception {
            return request("GET", path, Map.of());
        }

        /** Starts a login, lets the fake provider answer; returns {authorize URL, callback path}. */
        String[] toProvider(String provider, String query) throws Exception {
            HttpResponse<String> start = get("/auth/login/" + provider + query);
            assertEquals(302, start.statusCode(), "login redirects to the provider");
            String authorize = start.headers().firstValue("location").orElseThrow();
            HttpResponse<String> atIdp = HTTP.send(HttpRequest.newBuilder(URI.create(authorize)).build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(302, atIdp.statusCode(), "the provider accepted the authorize request: " + atIdp.body());
            URI back = URI.create(atIdp.headers().firstValue("location").orElseThrow());
            assertEquals(BASE_URL, back.getScheme() + "://" + back.getHost());
            return new String[] {authorize, back.getRawPath() + "?" + back.getRawQuery()};
        }

        HttpResponse<String> login(String provider, String query) throws Exception {
            return get(toProvider(provider, query)[1]);
        }

        HttpResponse<String> login(String provider) throws Exception {
            return login(provider, "");
        }

        JsonNode me() throws Exception {
            return JSON.readTree(get("/api/me").body());
        }
    }

    static Map<String, String> query(String url) {
        Map<String, String> out = new LinkedHashMap<>();
        for (String pair : URI.create(url).getRawQuery().split("&")) {
            int i = pair.indexOf('=');
            out.put(pair.substring(0, i), URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8));
        }
        return out;
    }

    static String error(HttpResponse<String> res) throws Exception {
        return JSON.readTree(res.body()).path("error").asText();
    }

    @Test
    void signedOutMeAndAdminAnswer401() throws Exception {
        Browser b = new Browser();
        assertEquals(401, b.get("/api/me").statusCode());
        assertEquals("signed_out", error(b.get("/api/me")));
        assertEquals(401, b.get("/api/admin").statusCode());
    }

    @Test
    void googleLoginRoundTripWithTheFakeProvider() throws Exception {
        IDP.user = LoginFakeIdp.person("g-100", "round@example.com", true, "Round");
        Browser b = new Browser();
        HttpResponse<String> done = b.login("google");
        assertEquals(302, done.statusCode());
        assertEquals("/", done.headers().firstValue("location").orElseThrow());
        JsonNode me = b.me();
        assertEquals("round@example.com", me.path("email").asText());
        assertEquals("[\"user\"]", me.path("roles").toString());
        assertEquals("[\"google\"]", me.path("providers").toString());
        assertTrue(me.path("csrf_token").asText().length() > 10);
    }

    @Test
    void githubLoginUsesTheVerifiedPrimaryEmail() throws Exception {
        IDP.user = LoginFakeIdp.person("gh-42", "grace@example.com", true, "grace");
        Browser b = new Browser();
        assertEquals(302, b.login("github").statusCode());
        JsonNode me = b.me();
        assertEquals("grace@example.com", me.path("email").asText());
        assertTrue(me.path("email_verified").asBoolean());
        assertEquals("[\"github\"]", me.path("providers").toString());
    }

    @Test
    void signInWithBossWorksThroughOidcDiscovery() throws Exception {
        IDP.user = LoginFakeIdp.person("b-1", "bossuser@example.com", true, "B");
        Browser b = new Browser();
        String ids = JSON.readTree(b.get("/auth/providers").body()).path("providers").findValuesAsText("id")
                .stream().sorted().collect(Collectors.joining(","));
        assertEquals("boss,github,google", ids);
        assertEquals(302, b.login("boss").statusCode());
        assertEquals(200, b.get("/api/me").statusCode());
    }

    @Test
    void aStateMismatchIsRejectedAndTheAttemptIsSpent() throws Exception {
        Browser b = new Browser();
        String callback = b.toProvider("google", "")[1];
        HttpResponse<String> res = b.get(callback.replace("state=", "state=forged"));
        assertEquals(400, res.statusCode());
        assertEquals("invalid_state", error(res));
        assertEquals(401, b.get("/api/me").statusCode());
        assertEquals(400, b.get(callback).statusCode(), "the real state cannot be replayed now");
    }

    @Test
    void aCallbackInABrowserThatNeverStartedTheLoginIsRejected() throws Exception {
        String callback = new Browser().toProvider("google", "")[1];
        Browser victim = new Browser();
        assertEquals(400, victim.get(callback).statusCode());
        assertEquals(401, victim.get("/api/me").statusCode());
    }

    @Test
    void pkceS256IsUsed() throws Exception {
        Browser b = new Browser();
        String[] flow = b.toProvider("google", "");
        assertEquals(302, b.get(flow[1]).statusCode());
        Map<String, String> q = query(flow[0]);
        assertEquals("S256", q.get("code_challenge_method"));
        String verifier = IDP.lastToken.get("code_verifier");
        assertNotNull(verifier);
        assertTrue(verifier.length() >= 43);
        String hashed = Base64.getUrlEncoder().withoutPadding().encodeToString(
                MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.US_ASCII)));
        assertEquals(q.get("code_challenge"), hashed);
        assertEquals(200, b.get("/api/me").statusCode());
    }

    @Test
    void anIdTokenWithAnotherNonceIsRejected() throws Exception {
        IDP.tamperNonce = "a-different-nonce";
        Browser b = new Browser();
        HttpResponse<String> res = b.login("google");
        assertEquals(400, res.statusCode());
        assertEquals("login_failed", error(res));
        assertEquals(401, b.get("/api/me").statusCode());
    }

    @Test
    void anUnverifiedEmailDoesNotLinkToAnExistingAccount() throws Exception {
        IDP.user = LoginFakeIdp.person("g-200", "victim@example.com", true, "Victim");
        assertEquals(302, new Browser().login("google").statusCode());
        IDP.user = LoginFakeIdp.person("gh-7", "victim@example.com", false, "mallory");
        Browser attacker = new Browser();
        HttpResponse<String> res = attacker.login("github");
        assertEquals(409, res.statusCode());
        assertEquals("account_exists_unverified_email", error(res));
        assertEquals(401, attacker.get("/api/me").statusCode());
    }

    @Test
    void aVerifiedEmailLinksASecondProvider() throws Exception {
        IDP.user = LoginFakeIdp.person("g-300", "linked@example.com", true, "Lin");
        Browser b = new Browser();
        b.login("google");
        String id = b.me().path("id").asText();
        IDP.user = LoginFakeIdp.person("gh-8", "linked@example.com", true, "lin");
        Browser again = new Browser();
        again.login("github");
        JsonNode me = again.me();
        assertEquals(id, me.path("id").asText());
        assertEquals("[\"google\",\"github\"]", me.path("providers").toString());
    }

    @Test
    void theSessionCookieFlagsAndRenewalAtSignIn() throws Exception {
        Browser b = new Browser();
        String callback = b.toProvider("google", "")[1];
        String before = b.jar.get("sid");
        b.get(callback);
        String after = b.jar.get("sid");
        assertNotNull(before);
        assertNotNull(after);
        assertNotEquals(before, after, "a new session id after sign-in (no fixation)");
        String cookie = b.setCookies.get(b.setCookies.size() - 1).toLowerCase();
        assertTrue(cookie.startsWith("sid="), cookie);
        assertTrue(cookie.contains("httponly"), cookie);
        assertTrue(cookie.contains("secure"), cookie);
        assertTrue(cookie.contains("samesite=lax"), cookie);
    }

    @Test
    void logoutNeedsTheCsrfTokenThenClearsTheSession() throws Exception {
        Browser b = new Browser();
        b.login("google");
        String csrf = b.me().path("csrf_token").asText();
        assertEquals(403, b.request("POST", "/auth/logout", Map.of()).statusCode());
        assertEquals(403, b.request("POST", "/auth/logout", Map.of("X-CSRF-Token", "wrong")).statusCode());
        assertEquals(200, b.get("/api/me").statusCode(), "a refused logout leaves the session");
        assertEquals(204, b.request("POST", "/auth/logout", Map.of("X-CSRF-Token", csrf)).statusCode());
        assertEquals(401, b.get("/api/me").statusCode());
    }

    @Test
    void theAdminRouteNeedsTheAdminRole() throws Exception {
        Browser user = new Browser();
        user.login("google");
        assertEquals(403, user.get("/api/admin").statusCode());
        IDP.user = LoginFakeIdp.person("g-boss", "boss@example.com", true, "Boss");
        Browser admin = new Browser();
        admin.login("google");
        assertEquals("[\"user\",\"admin\"]", admin.me().path("roles").toString());
        assertEquals(200, admin.get("/api/admin").statusCode());
    }

    @Test
    void returnToOnlyAcceptsALocalPath() throws Exception {
        assertEquals("/dashboard", new Browser().login("google", "?return_to=/dashboard").headers().firstValue("location").orElseThrow());
        for (String evil : List.of("https://evil.example", "//evil.example", "/\\evil.example")) {
            HttpResponse<String> res = new Browser().login("google", "?return_to=" + URLEncoder.encode(evil, StandardCharsets.UTF_8));
            assertEquals("/", res.headers().firstValue("location").orElseThrow(), evil);
        }
    }

    @Test
    void anUnknownProviderIsA404() throws Exception {
        Browser b = new Browser();
        assertEquals(404, b.get("/auth/login/facebook").statusCode());
        assertEquals(404, b.get("/auth/callback/facebook?code=x&state=y").statusCode());
    }
}
