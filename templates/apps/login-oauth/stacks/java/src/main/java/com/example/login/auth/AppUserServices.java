package com.example.login.auth;

import java.util.List;
import java.util.Map;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserRequest;
import org.springframework.security.oauth2.client.oidc.userinfo.OidcUserService;
import org.springframework.security.oauth2.client.userinfo.DefaultOAuth2UserService;
import org.springframework.security.oauth2.client.userinfo.OAuth2UserRequest;
import org.springframework.security.oauth2.client.userinfo.OAuth2UserService;
import org.springframework.security.oauth2.core.user.OAuth2User;
import org.springframework.security.oauth2.core.oidc.user.OidcUser;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/** Turns the provider's verified identity into the app's user (UserStore) and its roles. */
@Component
public class AppUserServices {
    private final UserStore store;
    private final OidcUserService oidc = new OidcUserService();
    private final DefaultOAuth2UserService oauth2 = new DefaultOAuth2UserService();
    private final RestClient rest = RestClient.create();

    public AppUserServices(UserStore store) {
        this.store = store;
    }

    /** Google and BOSS: the ID token is already checked (signature, iss, aud, exp, nonce). */
    public OAuth2UserService<OidcUserRequest, OidcUser> oidcService() {
        return request -> {
            OidcUser u = oidc.loadUser(request);
            String provider = request.getClientRegistration().getRegistrationId();
            AppUser user = store.signIn(provider, u.getSubject(), u.getEmail(),
                    Boolean.TRUE.equals(u.getEmailVerified()), u.getFullName(), u.getPicture());
            return new SignedInUser(user, store.rolesFor(user), u.getClaims(), u.getIdToken(), u.getUserInfo());
        };
    }

    /** GitHub: the profile from /user, the primary email and whether GitHub verified it. */
    public OAuth2UserService<OAuth2UserRequest, OAuth2User> oauth2Service() {
        return request -> {
            OAuth2User u = oauth2.loadUser(request);
            String provider = request.getClientRegistration().getRegistrationId();
            String emailsUri = request.getClientRegistration().getProviderDetails().getUserInfoEndpoint().getUri() + "/emails";
            List<Map<String, Object>> emails = List.of();
            try {
                emails = rest.get().uri(emailsUri)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + request.getAccessToken().getTokenValue())
                        .accept(MediaType.APPLICATION_JSON)
                        .retrieve().body(new ParameterizedTypeReference<>() {});
            } catch (RuntimeException e) {
                // no email scope granted: the account is keyed by the GitHub id alone
            }
            Map<String, Object> primary = emails == null ? null
                    : emails.stream().filter(e -> Boolean.TRUE.equals(e.get("primary"))).findFirst().orElse(null);
            String email = primary == null ? null : (String) primary.get("email");
            boolean verified = primary != null && Boolean.TRUE.equals(primary.get("verified"));
            Object name = u.getAttribute("name") != null ? u.getAttribute("name") : u.getAttribute("login");
            AppUser user = store.signIn(provider, String.valueOf(u.getAttributes().get("id")), email, verified,
                    name == null ? null : name.toString(), u.getAttribute("avatar_url"));
            return new SignedInUser(user, store.rolesFor(user), u.getAttributes(), null, null);
        };
    }
}
