package com.example.login.auth;

import java.io.Serializable;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.oauth2.core.oidc.OidcIdToken;
import org.springframework.security.oauth2.core.oidc.OidcUserInfo;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;

/** The principal of a signed-in session: the app's own user id plus its roles as authorities. */
public class SignedInUser implements OidcUser, Serializable {
    private final String appUserId;
    private final Map<String, Object> attributes;
    private final List<GrantedAuthority> authorities;
    private final OidcIdToken idToken;
    private final OidcUserInfo userInfo;

    SignedInUser(AppUser user, List<String> roles, Map<String, Object> attributes, OidcIdToken idToken,
                 OidcUserInfo userInfo) {
        this.appUserId = user.id;
        this.attributes = Map.copyOf(attributes);
        this.authorities = roles.stream()
                .map(r -> (GrantedAuthority) new SimpleGrantedAuthority("ROLE_" + r.toUpperCase())).toList();
        this.idToken = idToken;
        this.userInfo = userInfo;
    }

    public String appUserId() { return appUserId; }

    @Override public String getName() { return appUserId; }
    @Override public Map<String, Object> getAttributes() { return attributes; }
    @Override public Collection<? extends GrantedAuthority> getAuthorities() { return authorities; }
    @Override public Map<String, Object> getClaims() { return idToken != null ? idToken.getClaims() : attributes; }
    @Override public OidcUserInfo getUserInfo() { return userInfo; }
    @Override public OidcIdToken getIdToken() { return idToken; }
}
