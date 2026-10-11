package bank.auth;

import java.time.Instant;
import java.util.Optional;
import org.jdbi.v3.core.Jdbi;
import org.jdbi.v3.core.statement.UnableToExecuteStatementException;

/** The users table: create, look up, check and change a password. */
public final class Users {

    /** A stored user row; the hash never leaves the auth package. */
    record Row(long id, String username, String passwordHash) {}

    private final Jdbi jdbi;
    private final PasswordHasher hasher;
    /** Verified against when the username does not exist, so both cases cost the same time. */
    private final String decoyHash;

    public Users(Jdbi jdbi, PasswordHasher hasher) {
        this.jdbi = jdbi;
        this.hasher = hasher;
        this.decoyHash = hasher.hash(java.util.UUID.randomUUID().toString());
    }

    /** The new user's id, or empty when the username is taken. */
    public Optional<Long> create(String username, String password) {
        String hash = hasher.hash(password);
        try {
            return Optional.of(jdbi.withHandle(h -> h.createUpdate(
                    "INSERT INTO users (username, password_hash, created_at) "
                        + "VALUES (:username, :hash, :now)")
                .bind("username", username).bind("hash", hash)
                .bind("now", Instant.now().toString())
                .executeAndReturnGeneratedKeys("id").mapTo(Long.class).one()));
        } catch (UnableToExecuteStatementException e) {
            if (String.valueOf(e.getMessage()).contains("UNIQUE")) {
                return Optional.empty();
            }
            throw e;
        }
    }

    public boolean exists(String username) {
        return find(username).isPresent();
    }

    /** The user whose password matches, or empty. Unknown users and wrong passwords look alike. */
    public Optional<User> check(String username, String password) {
        Optional<Row> row = find(username);
        boolean ok = hasher.verify(password, row.map(Row::passwordHash).orElse(decoyHash));
        return ok && row.isPresent()
            ? Optional.of(new User(row.get().id(), row.get().username(), null))
            : Optional.empty();
    }

    public boolean passwordMatches(long userId, String password) {
        String hash = jdbi.withHandle(h -> h.createQuery(
                "SELECT password_hash FROM users WHERE id = :id")
            .bind("id", userId).mapTo(String.class).one());
        return hasher.verify(password, hash);
    }

    public void setPassword(long userId, String password) {
        String hash = hasher.hash(password);
        jdbi.useHandle(h -> h.createUpdate("UPDATE users SET password_hash = :hash WHERE id = :id")
            .bind("hash", hash).bind("id", userId).execute());
    }

    private Optional<Row> find(String username) {
        return jdbi.withHandle(h -> h.createQuery(
                "SELECT id, username, password_hash FROM users WHERE username = :username")
            .bind("username", username)
            .map((rs, ctx) -> new Row(rs.getLong("id"), rs.getString("username"),
                rs.getString("password_hash")))
            .findOne());
    }
}
