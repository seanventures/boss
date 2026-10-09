package com.example.login.auth;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.security.oauth2.core.OAuth2AuthenticationException;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.stereotype.Component;

/**
 * Users and their linked identities (schema.sql has the tables). This in-memory store is the
 * reference; back the same methods with the app's database (JPA, JDBC) when adapting the template.
 */
@Component
public class UserStore {
    public static final String ACCOUNT_EXISTS = "account_exists_unverified_email";

    private final Map<String, AppUser> users = new ConcurrentHashMap<>();
    private final Map<String, String> identities = new ConcurrentHashMap<>();
    private final AuthProperties props;

    public UserStore(AuthProperties props) {
        this.props = props;
    }

    public AppUser get(String id) {
        return id == null ? null : users.get(id);
    }

    public List<String> rolesFor(AppUser user) {
        List<String> roles = new ArrayList<>(List.of("user"));
        if (user.email != null && user.emailVerified && props.adminEmails().contains(user.email)) roles.add("admin");
        return roles;
    }

    /**
     * Account linking is {@code verified_email_only}: a new identity joins an existing account with
     * the same email ONLY when both the new provider and the existing account have that email
     * verified. Otherwise anyone who can register the address, unverified, at some provider could
     * take the account over (or pre-create it and wait for the owner to link into it).
     */
    public synchronized AppUser signIn(String provider, String subject, String email, boolean emailVerified,
                                       String name, String avatar) {
        String key = provider + ":" + subject;
        String normalised = email == null ? null : email.toLowerCase(Locale.ROOT);
        AppUser user = get(identities.get(key));
        if (user == null) {
            AppUser existing = normalised == null ? null
                    : users.values().stream().filter(u -> normalised.equals(u.email)).findFirst().orElse(null);
            if (existing != null) {
                if (!(emailVerified && existing.emailVerified)) {
                    throw new OAuth2AuthenticationException(new OAuth2Error(ACCOUNT_EXISTS));
                }
                user = existing;
            } else {
                user = new AppUser(UUID.randomUUID().toString(), normalised, emailVerified);
                users.put(user.id, user);
            }
            identities.put(key, user.id);
            if (!user.providers.contains(provider)) user.providers.add(provider);
        }
        if (name != null) user.name = name;
        if (avatar != null) user.avatar = avatar;
        return user;
    }
}
