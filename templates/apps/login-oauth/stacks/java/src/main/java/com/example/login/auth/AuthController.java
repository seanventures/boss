package com.example.login.auth;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.web.csrf.CsrfToken;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class AuthController {
    private final AuthProperties props;
    private final UserStore store;

    public AuthController(AuthProperties props, UserStore store) {
        this.props = props;
        this.store = store;
    }

    @GetMapping("/auth/providers")
    public Map<String, Object> providers() {
        List<Map<String, String>> list = props.providers().values().stream()
                .map(p -> Map.of("id", p.id(), "name", p.name(), "login_url", "/auth/login/" + p.id())).toList();
        return Map.of("providers", list);
    }

    @GetMapping("/api/me")
    public Map<String, Object> me(@AuthenticationPrincipal SignedInUser principal, CsrfToken csrf) {
        AppUser u = store.get(principal.appUserId());
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", u.id);
        out.put("email", u.email);
        out.put("email_verified", u.emailVerified);
        out.put("name", u.name);
        out.put("avatar", u.avatar);
        out.put("providers", List.copyOf(u.providers));
        out.put("roles", store.rolesFor(u));
        out.put("csrf_token", csrf.getToken());
        return out;
    }

    @GetMapping("/api/admin")
    public Map<String, Object> admin() {
        return Map.of("ok", true);
    }
}
