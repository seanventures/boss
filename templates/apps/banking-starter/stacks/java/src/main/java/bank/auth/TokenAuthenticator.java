package bank.auth;

import io.dropwizard.auth.Authenticator;
import java.util.Optional;

/** Resolves {@code Authorization: Bearer <token>} to a live session's user. */
public final class TokenAuthenticator implements Authenticator<String, User> {

    private final Sessions sessions;

    public TokenAuthenticator(Sessions sessions) {
        this.sessions = sessions;
    }

    @Override
    public Optional<User> authenticate(String token) {
        return sessions.authenticate(token);
    }
}
