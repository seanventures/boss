package bank.auth;

import io.dropwizard.auth.Authenticator;
import java.util.Optional;

/** Resolves {@code Authorization: Bearer <token>} to its user. */
public final class TokenAuthenticator implements Authenticator<String, User> {

    private final Tokens tokens;
    private final Users users;

    public TokenAuthenticator(Tokens tokens, Users users) {
        this.tokens = tokens;
        this.users = users;
    }

    @Override
    public Optional<User> authenticate(String token) {
        return tokens.verify(token).flatMap(users::byId);
    }
}
