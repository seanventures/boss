package com.example.login.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.security.oauth2.client.web.AuthorizationRequestRepository;
import org.springframework.security.oauth2.core.endpoint.OAuth2AuthorizationRequest;
import org.springframework.security.oauth2.core.endpoint.OAuth2ParameterNames;

/**
 * The login attempt in flight (state, nonce, PKCE verifier) kept in the server-side session, and
 * spent by the first callback whatever its state says: a forged or replayed callback also burns
 * the real attempt. The state is compared in constant time.
 */
public class OneShotAuthorizationRequests implements AuthorizationRequestRepository<OAuth2AuthorizationRequest> {
    private static final String KEY = "login-oauth.authorization-request";

    @Override
    public OAuth2AuthorizationRequest loadAuthorizationRequest(HttpServletRequest request) {
        HttpSession session = request.getSession(false);
        if (session == null) return null;
        OAuth2AuthorizationRequest stored = (OAuth2AuthorizationRequest) session.getAttribute(KEY);
        String state = request.getParameter(OAuth2ParameterNames.STATE);
        if (stored == null || state == null) return null;
        boolean same = MessageDigest.isEqual(state.getBytes(StandardCharsets.UTF_8),
                stored.getState().getBytes(StandardCharsets.UTF_8));
        return same ? stored : null;
    }

    @Override
    public void saveAuthorizationRequest(OAuth2AuthorizationRequest authorizationRequest, HttpServletRequest request,
                                         HttpServletResponse response) {
        if (authorizationRequest == null) {
            HttpSession session = request.getSession(false);
            if (session != null) session.removeAttribute(KEY);
            return;
        }
        request.getSession().setAttribute(KEY, authorizationRequest);
    }

    @Override
    public OAuth2AuthorizationRequest removeAuthorizationRequest(HttpServletRequest request, HttpServletResponse response) {
        OAuth2AuthorizationRequest matched = loadAuthorizationRequest(request);
        HttpSession session = request.getSession(false);
        if (session != null) session.removeAttribute(KEY);
        return matched;
    }
}
