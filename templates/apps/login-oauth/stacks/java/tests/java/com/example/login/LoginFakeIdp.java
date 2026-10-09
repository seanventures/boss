package com.example.login;

import com.nimbusds.jose.JWSAlgorithm;
import com.nimbusds.jose.JWSHeader;
import com.nimbusds.jose.crypto.RSASSASigner;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jose.jwk.gen.RSAKeyGenerator;
import com.nimbusds.jwt.JWTClaimsSet;
import com.nimbusds.jwt.SignedJWT;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * An in-process fake identity provider for the login-oauth tests: no network, no real Google,
 * GitHub or BOSS. One server plays OpenID Connect (discovery, /authorize, /token with an RS256 ID
 * token, /jwks, /userinfo) and GitHub's OAuth shape (/login/oauth/authorize,
 * /login/oauth/access_token, /api/user, /api/user/emails). It enforces a registered client with
 * the right secret, the exact redirect_uri, one-time codes and PKCE S256.
 */
public final class LoginFakeIdp {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final SecureRandom RANDOM = new SecureRandom();

    public final Map<String, String> clients = new ConcurrentHashMap<>();
    public volatile Map<String, Object> user = Map.of();
    public volatile String tamperNonce;
    public volatile Map<String, String> lastToken = Map.of();
    public final String issuer;

    private final Map<String, Map<String, Object>> codes = new ConcurrentHashMap<>();
    private final Map<String, Map<String, Object>> tokens = new ConcurrentHashMap<>();
    private final RSAKey key;
    private final HttpServer server;

    public LoginFakeIdp() throws Exception {
        key = new RSAKeyGenerator(2048).keyID("fake-kid").generate();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        issuer = "http://127.0.0.1:" + server.getAddress().getPort();
        server.createContext("/", this::handle);
        server.start();
    }

    public void close() {
        server.stop(0);
    }

    public static Map<String, Object> person(String sub, String email, boolean verified, String name) {
        Map<String, Object> m = new HashMap<>();
        m.put("sub", sub);
        m.put("email", email);
        m.put("email_verified", verified);
        m.put("name", name);
        return m;
    }

    private static String token() {
        byte[] b = new byte[24];
        RANDOM.nextBytes(b);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    private static Map<String, String> form(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) return out;
        for (String pair : raw.split("&")) {
            int i = pair.indexOf('=');
            String k = URLDecoder.decode(i < 0 ? pair : pair.substring(0, i), StandardCharsets.UTF_8);
            String v = i < 0 ? "" : URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8);
            out.put(k, v);
        }
        return out;
    }

    private static void send(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = JSON.writeValueAsBytes(body);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(status, data.length);
        ex.getResponseBody().write(data);
        ex.close();
    }

    private Map<String, Object> bearer(HttpExchange ex) {
        String auth = ex.getRequestHeaders().getFirst("Authorization");
        return auth != null && auth.regionMatches(true, 0, "Bearer ", 0, 7) ? tokens.get(auth.substring(7)) : null;
    }

    private void handle(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String route = ex.getRequestMethod() + " " + path;
            switch (route) {
                case "GET /.well-known/openid-configuration" -> send(ex, 200, Map.of(
                        "issuer", issuer, "authorization_endpoint", issuer + "/authorize",
                        "token_endpoint", issuer + "/token", "userinfo_endpoint", issuer + "/userinfo",
                        "jwks_uri", issuer + "/jwks", "response_types_supported", List.of("code"),
                        "subject_types_supported", List.of("public"),
                        "id_token_signing_alg_values_supported", List.of("RS256"),
                        "code_challenge_methods_supported", List.of("S256"),
                        "token_endpoint_auth_methods_supported", List.of("client_secret_basic", "client_secret_post")));
                case "GET /jwks" -> send(ex, 200, Map.of("keys", List.of(key.toPublicJWK().toJSONObject())));
                case "GET /authorize", "GET /login/oauth/authorize" -> authorize(ex);
                case "POST /token", "POST /login/oauth/access_token" -> token(ex);
                case "GET /userinfo" -> {
                    Map<String, Object> who = bearer(ex);
                    if (who == null) send(ex, 401, Map.of()); else send(ex, 200, who);
                }
                case "GET /api/user" -> {
                    Map<String, Object> who = bearer(ex);
                    if (who == null) { send(ex, 401, Map.of()); return; }
                    String digits = ((String) who.get("sub")).replaceAll("\\D", "");
                    Map<String, Object> u = new HashMap<>();
                    u.put("id", digits.isEmpty() ? 1 : Long.parseLong(digits));
                    u.put("login", who.get("name"));
                    u.put("name", who.get("name"));
                    send(ex, 200, u);
                }
                case "GET /api/user/emails" -> {
                    Map<String, Object> who = bearer(ex);
                    if (who == null) { send(ex, 401, Map.of()); return; }
                    send(ex, 200, List.of(Map.of("email", who.get("email"), "primary", true,
                            "verified", who.get("email_verified"))));
                }
                default -> send(ex, 404, Map.of("error", "not_found"));
            }
        } catch (Exception e) {
            send(ex, 500, Map.of("error", String.valueOf(e)));
        }
    }

    private void authorize(HttpExchange ex) throws IOException {
        Map<String, String> q = form(ex.getRequestURI().getRawQuery());
        if (!clients.containsKey(q.get("client_id")) || q.get("redirect_uri") == null || !"code".equals(q.get("response_type"))) {
            send(ex, 400, Map.of("error", "invalid_request"));
            return;
        }
        if (!"S256".equals(q.get("code_challenge_method")) || q.get("code_challenge") == null) {
            send(ex, 400, Map.of("error", "invalid_request", "error_description", "PKCE S256 required"));
            return;
        }
        String code = token();
        Map<String, Object> grant = new HashMap<>(q);
        grant.put("user", new HashMap<>(user));
        codes.put(code, grant);
        URI back = URI.create(q.get("redirect_uri"));
        String extra = "code=" + code + (q.containsKey("state") ? "&state=" + URLEncoder.encode(q.get("state"), StandardCharsets.UTF_8) : "");
        String location = back + (back.getRawQuery() == null ? "?" : "&") + extra;
        ex.getResponseHeaders().set("Location", location);
        ex.sendResponseHeaders(302, -1);
        ex.close();
    }

    @SuppressWarnings("unchecked")
    private void token(HttpExchange ex) throws Exception {
        Map<String, String> p = form(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        lastToken = p;
        String clientId = p.get("client_id");
        String secret = p.get("client_secret");
        String auth = ex.getRequestHeaders().getFirst("Authorization");
        if (auth != null && auth.startsWith("Basic ")) {
            String raw = new String(Base64.getDecoder().decode(auth.substring(6)), StandardCharsets.UTF_8);
            int i = raw.indexOf(':');
            clientId = URLDecoder.decode(raw.substring(0, i), StandardCharsets.UTF_8);
            secret = URLDecoder.decode(raw.substring(i + 1), StandardCharsets.UTF_8);
        }
        if (clientId == null || !clients.containsKey(clientId) || !clients.get(clientId).equals(secret)) {
            send(ex, 401, Map.of("error", "invalid_client"));
            return;
        }
        Map<String, Object> grant = codes.remove(String.valueOf(p.get("code")));
        if (!"authorization_code".equals(p.get("grant_type")) || grant == null
                || !clientId.equals(grant.get("client_id")) || !String.valueOf(grant.get("redirect_uri")).equals(p.get("redirect_uri"))) {
            send(ex, 400, Map.of("error", "invalid_grant"));
            return;
        }
        String verifier = p.getOrDefault("code_verifier", "");
        String challenge = Base64.getUrlEncoder().withoutPadding().encodeToString(
                MessageDigest.getInstance("SHA-256").digest(verifier.getBytes(StandardCharsets.US_ASCII)));
        if (!challenge.equals(grant.get("code_challenge"))) {
            send(ex, 400, Map.of("error", "invalid_grant", "error_description", "PKCE verification failed"));
            return;
        }
        Map<String, Object> who = (Map<String, Object>) grant.get("user");
        String access = token();
        tokens.put(access, who);
        Map<String, Object> out = new HashMap<>();
        out.put("access_token", access);
        out.put("token_type", "Bearer");
        out.put("expires_in", 3600);
        String scope = String.valueOf(grant.getOrDefault("scope", ""));
        out.put("scope", scope);
        if (List.of(scope.split(" ")).contains("openid")) {
            long now = System.currentTimeMillis();
            JWTClaimsSet.Builder claims = new JWTClaimsSet.Builder()
                    .issuer(issuer).audience(clientId).subject((String) who.get("sub"))
                    .issueTime(new Date(now)).expirationTime(new Date(now + 300_000))
                    .claim("nonce", tamperNonce != null ? tamperNonce : grant.get("nonce"))
                    .claim("email", who.get("email")).claim("email_verified", who.get("email_verified"))
                    .claim("name", who.get("name"));
            SignedJWT jwt = new SignedJWT(new JWSHeader.Builder(JWSAlgorithm.RS256).keyID(key.getKeyID()).build(), claims.build());
            jwt.sign(new RSASSASigner(key));
            out.put("id_token", jwt.serialize());
        }
        send(ex, 200, out);
    }

    /** Collected for debugging a failing test: the client ids the fake knows. */
    @Override
    public String toString() {
        return clients.keySet().stream().collect(Collectors.joining(","));
    }
}
