package bank;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.dropwizard.core.Configuration;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

/** config.yml. Every value has a safe default; none of them is a secret. */
public class BankConfiguration extends Configuration {

    /** The SQLite file. */
    @NotBlank
    private String databasePath = "bank.db";

    /** How long a bearer token lives (default 30 minutes). */
    @Min(1)
    private long tokenTtlSeconds = 1800;

    /** Failed logins allowed per username inside the window before logins answer 429. */
    @Min(1)
    private int loginMaxFailures = 5;

    @Min(1)
    private long loginWindowSeconds = 900;

    /** "production" turns off seeding of BOSS_TEST_USERS. */
    @NotBlank
    private String appEnv = "development";

    @JsonProperty public String getDatabasePath() { return databasePath; }
    @JsonProperty public void setDatabasePath(String v) { databasePath = v; }
    @JsonProperty public long getTokenTtlSeconds() { return tokenTtlSeconds; }
    @JsonProperty public void setTokenTtlSeconds(long v) { tokenTtlSeconds = v; }
    @JsonProperty public int getLoginMaxFailures() { return loginMaxFailures; }
    @JsonProperty public void setLoginMaxFailures(int v) { loginMaxFailures = v; }
    @JsonProperty public long getLoginWindowSeconds() { return loginWindowSeconds; }
    @JsonProperty public void setLoginWindowSeconds(long v) { loginWindowSeconds = v; }
    @JsonProperty public String getAppEnv() { return appEnv; }
    @JsonProperty public void setAppEnv(String v) { appEnv = v; }
}
