package bank.auth;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Failed-login throttling per username: after {@code maxFailures} failures inside
 * {@code window}, logins for that username answer 429 (even with the right password) until the
 * oldest failure leaves the window. A successful login clears the count.
 *
 * <p>In memory, per process. Behind several instances, move this to a shared store.
 */
public final class LoginRateLimiter {

    private static final int MAX_TRACKED = 10_000;

    private final int maxFailures;
    private final Duration window;
    private final Clock clock;
    private final Map<String, Deque<Instant>> failures = new ConcurrentHashMap<>();

    public LoginRateLimiter(int maxFailures, Duration window, Clock clock) {
        this.maxFailures = maxFailures;
        this.window = window;
        this.clock = clock;
    }

    /** Seconds until a login for {@code username} may be tried again; 0 when it may now. */
    public long retryAfterSeconds(String username) {
        Deque<Instant> q = failures.get(key(username));
        if (q == null) {
            return 0;
        }
        synchronized (q) {
            prune(q);
            if (q.size() < maxFailures) {
                return 0;
            }
            Instant reopens = q.peekFirst().plus(window);
            return Math.max(1, Duration.between(clock.instant(), reopens).toSeconds());
        }
    }

    public void recordFailure(String username) {
        if (failures.size() > MAX_TRACKED) {
            failures.entrySet().removeIf(e -> {
                synchronized (e.getValue()) {
                    prune(e.getValue());
                    return e.getValue().isEmpty();
                }
            });
        }
        Deque<Instant> q = failures.computeIfAbsent(key(username), k -> new ArrayDeque<>());
        synchronized (q) {
            prune(q);
            q.addLast(clock.instant());
        }
    }

    public void reset(String username) {
        failures.remove(key(username));
    }

    private void prune(Deque<Instant> q) {
        Instant cutoff = clock.instant().minus(window);
        while (!q.isEmpty() && !q.peekFirst().isAfter(cutoff)) {
            q.removeFirst();
        }
    }

    private static String key(String username) {
        return username == null ? "" : username.toLowerCase(Locale.ROOT);
    }
}
