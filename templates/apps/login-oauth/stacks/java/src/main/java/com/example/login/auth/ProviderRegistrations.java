package com.example.login.auth;

import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.security.oauth2.client.registration.ClientRegistration;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.registration.ClientRegistrations;
import org.springframework.security.oauth2.core.AuthorizationGrantType;
import org.springframework.security.oauth2.core.ClientAuthenticationMethod;
import org.springframework.stereotype.Component;

/**
 * The providers as Spring client registrations, built on first use and cached, so the app starts
 * without reaching any provider. Google and BOSS use OIDC discovery; GitHub has fixed endpoints.
 */
@Component
public class ProviderRegistrations implements ClientRegistrationRepository, Iterable<ClientRegistration> {
    private final AuthProperties props;
    private final Map<String, ClientRegistration> cache = new ConcurrentHashMap<>();

    public ProviderRegistrations(AuthProperties props) {
        this.props = props;
    }

    @Override
    public ClientRegistration findByRegistrationId(String id) {
        AuthProperties.Provider p = props.providers().get(id);
        if (p == null) return null;
        return cache.computeIfAbsent(id, k -> build(p));
    }

    private ClientRegistration build(AuthProperties.Provider p) {
        ClientRegistration.Builder b;
        if ("oidc".equals(p.kind())) {
            b = ClientRegistrations.fromIssuerLocation(p.issuer()).scope("openid", "email", "profile");
        } else {
            b = ClientRegistration.withRegistrationId(p.id())
                    .authorizationUri(p.issuer() + "/login/oauth/authorize")
                    .tokenUri(p.issuer() + "/login/oauth/access_token")
                    .userInfoUri(p.apiUrl() + "/user")
                    .userNameAttributeName("id")
                    .scope("read:user", "user:email");
        }
        return b.registrationId(p.id())
                .clientName(p.name())
                .clientId(p.clientId())
                .clientSecret(p.clientSecret())
                .clientAuthenticationMethod(ClientAuthenticationMethod.CLIENT_SECRET_BASIC)
                .authorizationGrantType(AuthorizationGrantType.AUTHORIZATION_CODE)
                .redirectUri(props.redirectUri(p.id()))
                .build();
    }

    @Override
    public Iterator<ClientRegistration> iterator() {
        return props.providers().keySet().stream().map(this::findByRegistrationId).iterator();
    }
}
