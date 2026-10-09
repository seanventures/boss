package com.example.login.auth;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * Configuration for the login-oauth template, read from the environment. Client ids and secrets
 * are app secrets: BOSS puts them in the environment at run time from its vault. Never write one
 * into code, a properties file or a test. (SESSION_SECRET is not needed here: the servlet
 * container's session ids are random server-side ids, not signed cookies.)
 */
@Component
public class AuthProperties {

    /** One sign-in provider. kind is "oidc" (discovery) or "oauth2" (GitHub). */
    public record Provider(String id, String kind, String name, String clientId, String clientSecret,
                           String issuer, String apiUrl) {}

    private final String baseUrl;
    private final Set<String> adminEmails;
    private final Map<String, Provider> providers = new LinkedHashMap<>();

    public AuthProperties(Environment env) {
        this.baseUrl = get(env, "APP_BASE_URL", "http://localhost:3000").replaceAll("/+$", "");
        this.adminEmails = Arrays.stream(get(env, "AUTH_ADMIN_EMAILS", "").split(","))
                .map(s -> s.trim().toLowerCase(Locale.ROOT)).filter(s -> !s.isEmpty()).collect(Collectors.toSet());
        if (!get(env, "GOOGLE_CLIENT_ID", "").isEmpty()) {
            providers.put("google", new Provider("google", "oidc", "Google", get(env, "GOOGLE_CLIENT_ID", ""),
                    get(env, "GOOGLE_CLIENT_SECRET", ""), get(env, "GOOGLE_ISSUER", "https://accounts.google.com"), null));
        }
        if (!get(env, "GITHUB_CLIENT_ID", "").isEmpty()) {
            providers.put("github", new Provider("github", "oauth2", "GitHub", get(env, "GITHUB_CLIENT_ID", ""),
                    get(env, "GITHUB_CLIENT_SECRET", ""), get(env, "GITHUB_URL", "https://github.com").replaceAll("/+$", ""),
                    get(env, "GITHUB_API_URL", "https://api.github.com").replaceAll("/+$", "")));
        }
        // "Sign in with BOSS": any OIDC issuer with discovery; BOSS fills these when the app is
        // registered as a client of its OIDC provider.
        if (!get(env, "OIDC_BOSS_ISSUER", "").isEmpty() && !get(env, "OIDC_BOSS_CLIENT_ID", "").isEmpty()) {
            providers.put("boss", new Provider("boss", "oidc", "BOSS", get(env, "OIDC_BOSS_CLIENT_ID", ""),
                    get(env, "OIDC_BOSS_CLIENT_SECRET", ""), get(env, "OIDC_BOSS_ISSUER", "").replaceAll("/+$", ""), null));
        }
    }

    private static String get(Environment env, String name, String fallback) {
        String v = env.getProperty(name);
        return v == null || v.isBlank() ? fallback : v.trim();
    }

    public String baseUrl() { return baseUrl; }
    public boolean secureCookies() { return baseUrl.startsWith("https://"); }
    public Set<String> adminEmails() { return adminEmails; }
    public Map<String, Provider> providers() { return providers; }
    public String redirectUri(String id) { return baseUrl + "/auth/callback/" + id; }
}
