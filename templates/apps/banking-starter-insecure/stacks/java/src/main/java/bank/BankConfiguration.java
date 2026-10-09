package bank;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.dropwizard.core.Configuration;
import jakarta.validation.constraints.NotBlank;

/** config.yml. */
public class BankConfiguration extends Configuration {

    /** The SQLite file. */
    @NotBlank
    private String databasePath = "bank.db";

    /** "production" turns off seeding of BOSS_TEST_USERS. */
    @NotBlank
    private String appEnv = "development";

    @JsonProperty public String getDatabasePath() { return databasePath; }
    @JsonProperty public void setDatabasePath(String v) { databasePath = v; }
    @JsonProperty public String getAppEnv() { return appEnv; }
    @JsonProperty public void setAppEnv(String v) { appEnv = v; }
}
