package bank.auth;

import java.security.Principal;

/** The signed-in user, as the token filter resolved it. {@code tokenHash} names the session. */
public record User(long id, String username, String tokenHash) implements Principal {
    @Override
    public String getName() {
        return username;
    }
}
