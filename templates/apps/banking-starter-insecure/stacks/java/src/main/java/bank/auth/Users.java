package bank.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.Optional;
import org.jdbi.v3.core.Jdbi;
import org.jdbi.v3.core.statement.UnableToExecuteStatementException;

/** The users table. */
public final class Users {

    /** The outcome of a login attempt. */
    public enum Check { OK, NO_SUCH_USER, WRONG_PASSWORD }

    public record Result(Check check, User user) {}

    private record Row(long id, String username, String hash) {}

    private final Jdbi jdbi;

    public Users(Jdbi jdbi) {
        this.jdbi = jdbi;
    }

    /** The new user's id, or empty when the username is taken. */
    public Optional<Long> create(String username, String password) {
        try {
            return Optional.of(jdbi.withHandle(h -> h.createUpdate(
                    "INSERT INTO users (username, password_hash, created_at) "
                        + "VALUES (:username, :hash, :now)")
                .bind("username", username).bind("hash", digest(password))
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
        return jdbi.withHandle(h -> h.createQuery("SELECT COUNT(*) FROM users WHERE username = :u")
            .bind("u", username).mapTo(Integer.class).one()) > 0;
    }

    public Result check(String username, String password) {
        Optional<Row> row = jdbi.withHandle(h -> h.createQuery(
                "SELECT id, username, password_hash FROM users WHERE username = :username")
            .bind("username", username)
            .map((rs, ctx) -> new Row(rs.getLong("id"), rs.getString("username"),
                rs.getString("password_hash")))
            .findOne());
        if (row.isEmpty()) {
            return new Result(Check.NO_SUCH_USER, null);
        }
        if (!row.get().hash().equals(digest(password))) {
            return new Result(Check.WRONG_PASSWORD, null);
        }
        return new Result(Check.OK, new User(row.get().id(), row.get().username(), null));
    }

    public Optional<User> byId(long id) {
        return jdbi.withHandle(h -> h.createQuery("SELECT id, username FROM users WHERE id = :id")
            .bind("id", id)
            .map((rs, ctx) -> new User(rs.getLong("id"), rs.getString("username"), null))
            .findOne());
    }

    public void setPassword(long userId, String password) {
        jdbi.useHandle(h -> h.createUpdate("UPDATE users SET password_hash = :hash WHERE id = :id")
            .bind("hash", digest(password)).bind("id", userId).execute());
    }

    static String digest(String password) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            return HexFormat.of().formatHex(md.digest(password.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
