package bank.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Clock;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Optional;
import org.jdbi.v3.core.Jdbi;

/**
 * Opaque bearer tokens: 256 random bits, given to the client once. The database keeps only the
 * token's SHA-256, its user and its expiry, so a leaked database holds no usable token. A token
 * stops working when it expires, on logout, or when its user changes the password (every other
 * session of that user is revoked).
 *
 * <p>Why not a JWT: an opaque token is revocable at once and has no signing key to manage.
 */
public final class Sessions {

    private static final SecureRandom RANDOM = new SecureRandom();

    private final Jdbi jdbi;
    private final long ttlSeconds;
    private final Clock clock;

    public Sessions(Jdbi jdbi, long ttlSeconds, Clock clock) {
        this.jdbi = jdbi;
        this.ttlSeconds = ttlSeconds;
        this.clock = clock;
    }

    public record Issued(String token, long expiresAt) {}

    public Issued issue(long userId) {
        byte[] raw = new byte[32];
        RANDOM.nextBytes(raw);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(raw);
        long now = clock.instant().getEpochSecond();
        long expires = now + ttlSeconds;
        jdbi.useHandle(h -> h.createUpdate(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) "
                    + "VALUES (:hash, :user, :expires, :now)")
            .bind("hash", hash(token)).bind("user", userId)
            .bind("expires", expires).bind("now", now).execute());
        return new Issued(token, expires);
    }

    public Optional<User> authenticate(String token) {
        if (token == null || token.length() != 43) {
            return Optional.empty();
        }
        String tokenHash = hash(token);
        long now = clock.instant().getEpochSecond();
        return jdbi.withHandle(h -> h.createQuery(
                "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
                    + "WHERE s.token_hash = :hash AND s.expires_at > :now")
            .bind("hash", tokenHash).bind("now", now)
            .map((rs, ctx) -> new User(rs.getLong("id"), rs.getString("username"), tokenHash))
            .findOne());
    }

    public void revoke(String tokenHash) {
        jdbi.useHandle(h -> h.createUpdate("DELETE FROM sessions WHERE token_hash = :hash")
            .bind("hash", tokenHash).execute());
    }

    /** Revokes every session of the user except {@code keepHash}. */
    public void revokeOthers(long userId, String keepHash) {
        jdbi.useHandle(h -> h.createUpdate(
                "DELETE FROM sessions WHERE user_id = :user AND token_hash <> :keep")
            .bind("user", userId).bind("keep", keepHash).execute());
    }

    public void purgeExpired() {
        long now = clock.instant().getEpochSecond();
        jdbi.useHandle(h -> h.createUpdate("DELETE FROM sessions WHERE expires_at <= :now")
            .bind("now", now).execute());
    }

    static String hash(String token) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(token.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
