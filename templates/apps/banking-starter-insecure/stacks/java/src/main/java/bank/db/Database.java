package bank.db;

import org.jdbi.v3.core.Jdbi;
import org.sqlite.SQLiteConfig;
import org.sqlite.SQLiteDataSource;

/** The SQLite database and its schema. */
public final class Database {

    private static final String[] SCHEMA = {
        """
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          username      TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at    TEXT NOT NULL
        )""",
        """
        CREATE TABLE IF NOT EXISTS accounts (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id    INTEGER NOT NULL REFERENCES users(id),
          currency   TEXT NOT NULL CHECK (length(currency) = 3),
          created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS accounts_by_user ON accounts(user_id)",
        """
        CREATE TABLE IF NOT EXISTS ledger_entries (
          id              INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id      INTEGER NOT NULL REFERENCES accounts(id),
          amount_cents    INTEGER NOT NULL,
          kind            TEXT NOT NULL
                          CHECK (kind IN ('deposit', 'withdrawal', 'transfer_in', 'transfer_out')),
          idempotency_key TEXT,
          created_at      TEXT NOT NULL,
          UNIQUE (account_id, idempotency_key)
        )""",
        "CREATE INDEX IF NOT EXISTS ledger_by_account ON ledger_entries(account_id)",
    };

    private Database() {}

    public static Jdbi open(String path) {
        SQLiteConfig config = new SQLiteConfig();
        config.enforceForeignKeys(true);
        config.setBusyTimeout(15_000);
        config.setJournalMode(SQLiteConfig.JournalMode.WAL);
        config.setTransactionMode(SQLiteConfig.TransactionMode.IMMEDIATE);
        SQLiteDataSource ds = new SQLiteDataSource(config);
        ds.setUrl("jdbc:sqlite:" + path);
        Jdbi jdbi = Jdbi.create(ds);
        jdbi.useHandle(h -> {
            for (String statement : SCHEMA) {
                h.execute(statement);
            }
        });
        return jdbi;
    }
}
