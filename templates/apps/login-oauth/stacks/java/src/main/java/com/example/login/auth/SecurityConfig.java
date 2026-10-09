package com.example.login.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import java.io.IOException;
import org.springframework.boot.web.server.WebServerFactoryCustomizer;
import org.springframework.boot.web.servlet.server.ConfigurableServletWebServerFactory;
import org.springframework.boot.web.servlet.server.AbstractServletWebServerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestCustomizers;
import org.springframework.security.oauth2.client.web.OAuth2AuthorizationRequestResolver;
import org.springframework.security.oauth2.core.OAuth2AuthenticationException;
import org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.csrf.CsrfException;
import org.springframework.security.web.csrf.CsrfTokenRequestAttributeHandler;
import org.springframework.security.web.csrf.HttpSessionCsrfTokenRepository;

/**
 * The login-oauth routes on Spring Security:
 * <pre>
 *   GET  /auth/providers           the configured providers, for the login buttons (AuthController)
 *   GET  /auth/login/{provider}    redirect to the provider (state + PKCE S256 + nonce)
 *   GET  /auth/callback/{provider} check state, exchange the code, check the ID token, sign in
 *   POST /auth/logout              CSRF-checked; invalidates the session
 *   GET  /api/me                   the signed-in user (401 when signed out) (AuthController)
 *   GET  /api/admin                only the ADMIN role (AuthController)
 * </pre>
 * Protect the app's own routes in {@link #filterChain} ({@code .authenticated()},
 * {@code .hasRole("ADMIN")}).
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {
    static final String RETURN_TO = "login-oauth.return-to";

    @Bean
    SecurityFilterChain filterChain(HttpSecurity http, ProviderRegistrations registrations,
                                    AppUserServices users) throws Exception {
        HttpSessionCsrfTokenRepository csrfTokens = new HttpSessionCsrfTokenRepository();
        csrfTokens.setHeaderName("X-CSRF-Token");
        http
            .authorizeHttpRequests(a -> a
                .requestMatchers("/api/admin").hasRole("ADMIN")
                .requestMatchers("/api/**").authenticated()
                .anyRequest().permitAll())
            .oauth2Login(o -> o
                .loginPage("/auth/providers")
                .clientRegistrationRepository(registrations)
                .authorizationEndpoint(e -> e
                    .authorizationRequestResolver(resolver(registrations))
                    .authorizationRequestRepository(new OneShotAuthorizationRequests()))
                .redirectionEndpoint(r -> r.baseUri("/auth/callback/*"))
                .userInfoEndpoint(u -> u.oidcUserService(users.oidcService()).userService(users.oauth2Service()))
                .successHandler((req, res, auth) -> {
                    HttpSession session = req.getSession();
                    Object to = session.getAttribute(RETURN_TO);
                    session.removeAttribute(RETURN_TO);
                    res.setStatus(302);
                    res.setHeader("Location", to instanceof String s ? s : "/");
                })
                .failureHandler((req, res, ex) -> loginFailed(req, res, ex, registrations)))
            .logout(l -> l
                .logoutUrl("/auth/logout")
                .invalidateHttpSession(true)
                .deleteCookies("sid")
                .logoutSuccessHandler((req, res, auth) -> res.setStatus(204)))
            // Sign-out and any other state-changing request carry the X-CSRF-Token header with the
            // value /api/me hands out (plain token, kept in the server-side session).
            .csrf(c -> c.csrfTokenRepository(csrfTokens).csrfTokenRequestHandler(new CsrfTokenRequestAttributeHandler()))
            .exceptionHandling(e -> e
                .authenticationEntryPoint((req, res, ex) -> json(res, 401, "signed_out"))
                .accessDeniedHandler((req, res, ex) -> json(res, 403, ex instanceof CsrfException ? "csrf" : "forbidden")));
        return http.build();
    }

    /** PKCE S256 on every provider (Spring adds it only for public clients by default), the
     *  return_to path remembered, and an unknown provider left to answer 404. */
    private static OAuth2AuthorizationRequestResolver resolver(ProviderRegistrations registrations) {
        DefaultOAuth2AuthorizationRequestResolver delegate =
                new DefaultOAuth2AuthorizationRequestResolver(registrations, "/auth/login");
        delegate.setAuthorizationRequestCustomizer(OAuth2AuthorizationRequestCustomizers.withPkce());
        return new OAuth2AuthorizationRequestResolver() {
            @Override
            public OAuth2AuthorizationRequest resolve(HttpServletRequest request) {
                String uri = request.getRequestURI();
                if (!uri.startsWith("/auth/login/")) return null;
                if (registrations.findByRegistrationId(uri.substring("/auth/login/".length())) == null) return null;
                OAuth2AuthorizationRequest r = delegate.resolve(request);
                if (r != null) request.getSession().setAttribute(RETURN_TO, safeReturnTo(request.getParameter("return_to")));
                return r;
            }

            @Override
            public OAuth2AuthorizationRequest resolve(HttpServletRequest request, String registrationId) {
                return registrations.findByRegistrationId(registrationId) == null ? null : delegate.resolve(request, registrationId);
            }
        };
    }

    /** Only a path on this app may be a post-login destination (no open redirect). */
    static String safeReturnTo(String value) {
        if (value == null || !value.startsWith("/") || value.startsWith("//") || value.contains("\\")) return "/";
        return value;
    }

    private static void loginFailed(HttpServletRequest req, HttpServletResponse res, AuthenticationException ex,
                                    ProviderRegistrations registrations) throws IOException {
        String id = req.getRequestURI().substring(req.getRequestURI().lastIndexOf('/') + 1);
        if (registrations.findByRegistrationId(id) == null) {
            json(res, 404, "unknown_provider");
            return;
        }
        String code = ex instanceof OAuth2AuthenticationException o ? o.getError().getErrorCode() : "";
        switch (code) {
            case "authorization_request_not_found", "invalid_state_parameter" -> json(res, 400, "invalid_state");
            case UserStore.ACCOUNT_EXISTS -> json(res, 409, UserStore.ACCOUNT_EXISTS);
            case "access_denied" -> json(res, 400, "provider_refused");
            default -> json(res, 400, "login_failed");
        }
    }

    static void json(HttpServletResponse res, int status, String error) throws IOException {
        res.setStatus(status);
        res.setContentType("application/json");
        res.getWriter().write("{\"error\":\"" + error + "\"}");
    }

    /** The session cookie is Secure whenever the app is served over https (APP_BASE_URL). */
    @Bean
    WebServerFactoryCustomizer<ConfigurableServletWebServerFactory> secureSessionCookie(AuthProperties props) {
        return factory -> {
            if (factory instanceof AbstractServletWebServerFactory f) f.getSession().getCookie().setSecure(props.secureCookies());
        };
    }
}
