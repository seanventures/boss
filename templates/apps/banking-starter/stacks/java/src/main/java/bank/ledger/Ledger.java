package bank.ledger;

import bank.web.Errors;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.jdbi.v3.core.Handle;
import org.jdbi.v3.core.Jdbi;

/**
 * Accounts and their ledger. The balance is always {@code SUM} over the account's entries; there
 * is no balance column to drift or to race on.
 *
 * <p>Every method takes the caller's user id and finds the account with
 * {@code WHERE id = :id AND user_id = :user}: an account that is not the caller's is "not found",
 * exactly like one that does not exist.
 *
 * <p>Each movement runs in one IMMEDIATE transaction (see Database): ownership check, idempotency
 * check, balance check and insert happen under SQLite's write lock, so concurrent withdrawals
 * cannot overdraw.
 */
public final class Ledger {

    public record Account(
        @JsonProperty("id") long id,
        @JsonProperty("currency") String currency,
        @JsonProperty("created_at") String createdAt,
        @JsonProperty("balance_cents") long balanceCents) {}

    public record Entry(
        @JsonProperty("id") long id,
        @JsonProperty("account_id") long accountId,
        @JsonProperty("kind") String kind,
        @JsonProperty("amount_cents") long amountCents,
        @JsonProperty("idempotency_key") String idempotencyKey,
        @JsonProperty("created_at") String createdAt) {}

    /** A posted movement: the entry, the balance after it, and whether it was a replay. */
    public record Posted(
        @JsonProperty("entry") Entry entry,
        @JsonProperty("balance_cents") long balanceCents,
        @JsonProperty("replayed") boolean replayed) {}

    public record Transferred(
        @JsonProperty("from") Entry from,
        @JsonProperty("to") Entry to,
        @JsonProperty("from_balance_cents") long fromBalanceCents,
        @JsonProperty("to_balance_cents") long toBalanceCents,
        @JsonProperty("replayed") boolean replayed) {}

    private static final String ENTRY_COLUMNS =
        "id, account_id, kind, amount_cents, idempotency_key, created_at";

    private final Jdbi jdbi;

    public Ledger(Jdbi jdbi) {
        this.jdbi = jdbi;
    }

    public Account open(long userId, String currency) {
        return jdbi.inTransaction(h -> {
            long id = h.createUpdate(
                    "INSERT INTO accounts (user_id, currency, created_at) VALUES (:user, :cur, :now)")
                .bind("user", userId).bind("cur", currency).bind("now", Instant.now().toString())
                .executeAndReturnGeneratedKeys("id").mapTo(Long.class).one();
            return owned(h, userId, id);
        });
    }

    public List<Account> list(long userId) {
        return jdbi.withHandle(h -> h.createQuery(
                "SELECT id FROM accounts WHERE user_id = :user ORDER BY id")
            .bind("user", userId).mapTo(Long.class).list()
            .stream().map(id -> owned(h, userId, id)).toList());
    }

    public Account get(long userId, long accountId) {
        return jdbi.withHandle(h -> owned(h, userId, accountId));
    }

    public List<Entry> entries(long userId, long accountId) {
        return jdbi.withHandle(h -> {
            owned(h, userId, accountId);
            return h.createQuery("SELECT " + ENTRY_COLUMNS
                    + " FROM ledger_entries WHERE account_id = :acct ORDER BY id")
                .bind("acct", accountId).map((rs, ctx) -> entry(rs)).list();
        });
    }

    public Posted deposit(long userId, long accountId, long amountCents, String key) {
        return post(userId, accountId, "deposit", amountCents, key);
    }

    public Posted withdraw(long userId, long accountId, long amountCents, String key) {
        return post(userId, accountId, "withdrawal", amountCents, key);
    }

    private Posted post(long userId, long accountId, String kind, long amountCents, String key) {
        return jdbi.inTransaction(h -> {
            owned(h, userId, accountId);
            Optional<Entry> earlier = byKey(h, accountId, key);
            if (earlier.isPresent()) {
                Entry e = earlier.get();
                if (!e.kind().equals(kind) || e.amountCents() != amountCents) {
                    throw Errors.of(422, "idempotency_key was already used for a different request");
                }
                return new Posted(e, balance(h, accountId), true);
            }
            if (kind.equals("withdrawal") && balance(h, accountId) < amountCents) {
                throw Errors.of(409, "insufficient funds");
            }
            Entry e = insert(h, accountId, kind, amountCents, key);
            return new Posted(e, balance(h, accountId), false);
        });
    }

    public Transferred transfer(long userId, long fromId, long toId, long amountCents, String key) {
        if (fromId == toId) {
            throw Errors.of(422, "from_account_id and to_account_id must differ");
        }
        return jdbi.inTransaction(h -> {
            Account from = owned(h, userId, fromId);
            Account to = owned(h, userId, toId);
            if (!from.currency().equals(to.currency())) {
                throw Errors.of(422, "accounts have different currencies");
            }
            Optional<Entry> earlier = byKey(h, fromId, key);
            if (earlier.isPresent()) {
                Entry e = earlier.get();
                if (!e.kind().equals("transfer_out") || e.amountCents() != amountCents) {
                    throw Errors.of(422, "idempotency_key was already used for a different request");
                }
                Entry in = byKey(h, toId, key).orElseThrow();
                return new Transferred(e, in, balance(h, fromId), balance(h, toId), true);
            }
            if (byKey(h, toId, key).isPresent()) {
                throw Errors.of(422, "idempotency_key was already used for a different request");
            }
            if (balance(h, fromId) < amountCents) {
                throw Errors.of(409, "insufficient funds");
            }
            Entry out = insert(h, fromId, "transfer_out", amountCents, key);
            Entry in = insert(h, toId, "transfer_in", amountCents, key);
            return new Transferred(out, in, balance(h, fromId), balance(h, toId), false);
        });
    }

    /** The caller's account, or 404. */
    private static Account owned(Handle h, long userId, long accountId) {
        return h.createQuery("SELECT id, currency, created_at FROM accounts "
                + "WHERE id = :id AND user_id = :user")
            .bind("id", accountId).bind("user", userId)
            .map((rs, ctx) -> new Account(rs.getLong("id"), rs.getString("currency"),
                rs.getString("created_at"), 0))
            .findOne()
            .map(a -> new Account(a.id(), a.currency(), a.createdAt(), balance(h, a.id())))
            .orElseThrow(Errors::accountNotFound);
    }

    /** Credits minus debits, from the ledger. */
    static long balance(Handle h, long accountId) {
        return h.createQuery("SELECT COALESCE(SUM(CASE WHEN kind IN ('deposit', 'transfer_in') "
                + "THEN amount_cents ELSE -amount_cents END), 0) "
                + "FROM ledger_entries WHERE account_id = :acct")
            .bind("acct", accountId).mapTo(Long.class).one();
    }

    private static Optional<Entry> byKey(Handle h, long accountId, String key) {
        if (key == null) {
            return Optional.empty();
        }
        return h.createQuery("SELECT " + ENTRY_COLUMNS
                + " FROM ledger_entries WHERE account_id = :acct AND idempotency_key = :key")
            .bind("acct", accountId).bind("key", key).map((rs, ctx) -> entry(rs)).findOne();
    }

    private static Entry insert(Handle h, long accountId, String kind, long amountCents, String key) {
        long id = h.createUpdate("INSERT INTO ledger_entries "
                + "(account_id, amount_cents, kind, idempotency_key, created_at) "
                + "VALUES (:acct, :amount, :kind, :key, :now)")
            .bind("acct", accountId).bind("amount", amountCents).bind("kind", kind)
            .bind("key", key).bind("now", Instant.now().toString())
            .executeAndReturnGeneratedKeys("id").mapTo(Long.class).one();
        return h.createQuery("SELECT " + ENTRY_COLUMNS + " FROM ledger_entries WHERE id = :id")
            .bind("id", id).map((rs, ctx) -> entry(rs)).one();
    }

    private static Entry entry(java.sql.ResultSet rs) throws java.sql.SQLException {
        return new Entry(rs.getLong("id"), rs.getLong("account_id"), rs.getString("kind"),
            rs.getLong("amount_cents"), rs.getString("idempotency_key"), rs.getString("created_at"));
    }
}
