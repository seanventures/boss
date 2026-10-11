package bank;

import bank.api.AccountResource;
import bank.api.AuthResource;
import bank.api.MetaResource;
import bank.auth.LoginRateLimiter;
import bank.auth.PasswordHasher;
import bank.auth.Sessions;
import bank.auth.TokenAuthenticator;
import bank.auth.User;
import bank.auth.Users;
import bank.db.Database;
import bank.ledger.Ledger;
import bank.web.SecurityHeadersFilter;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;
import com.fasterxml.jackson.databind.type.LogicalType;
import io.dropwizard.auth.AuthDynamicFeature;
import io.dropwizard.auth.AuthValueFactoryProvider;
import io.dropwizard.auth.oauth.OAuthCredentialAuthFilter;
import io.dropwizard.configuration.EnvironmentVariableSubstitutor;
import io.dropwizard.configuration.SubstitutingSourceProvider;
import io.dropwizard.core.Application;
import io.dropwizard.core.setup.Bootstrap;
import io.dropwizard.core.setup.Environment;
import java.time.Clock;
import java.time.Duration;
import org.jdbi.v3.core.Jdbi;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** The banking starter: {@code java -jar target/banking-starter-1.0.0.jar server config.yml}. */
public class BankApplication extends Application<BankConfiguration> {

    private static final Logger LOG = LoggerFactory.getLogger(BankApplication.class);

    public static void main(String[] args) throws Exception {
        new BankApplication().run(args);
    }

    @Override
    public String getName() {
        return "banking-starter";
    }

    @Override
    public void initialize(Bootstrap<BankConfiguration> bootstrap) {
        // ${PORT:-8080} etc. in config.yml come from the environment (non-strict: defaults apply).
        bootstrap.setConfigurationSourceProvider(new SubstitutingSourceProvider(
            bootstrap.getConfigurationSourceProvider(), new EnvironmentVariableSubstitutor(false)));
    }

    @Override
    public void run(BankConfiguration config, Environment env) {
        strictJson(env.getObjectMapper());

        Jdbi jdbi = Database.open(config.getDatabasePath());
        Clock clock = Clock.systemUTC();
        PasswordHasher hasher = new PasswordHasher();
        Users users = new Users(jdbi, hasher);
        Sessions sessions = new Sessions(jdbi, config.getTokenTtlSeconds(), clock);
        sessions.purgeExpired();
        LoginRateLimiter limiter = new LoginRateLimiter(config.getLoginMaxFailures(),
            Duration.ofSeconds(config.getLoginWindowSeconds()), clock);

        int seeded = TestUsers.seed(System.getenv("BOSS_TEST_USERS"), config.getAppEnv(), users,
            env.getObjectMapper());
        if (seeded > 0) {
            LOG.info("seeded {} test user(s) from BOSS_TEST_USERS", seeded);
        }

        env.jersey().register(new AuthDynamicFeature(new OAuthCredentialAuthFilter.Builder<User>()
            .setAuthenticator(new TokenAuthenticator(sessions))
            .setPrefix("Bearer")
            .setRealm("bank")
            .buildAuthFilter()));
        env.jersey().register(new AuthValueFactoryProvider.Binder<>(User.class));
        env.jersey().register(new SecurityHeadersFilter());
        env.jersey().register(new MetaResource());
        env.jersey().register(new AuthResource(users, sessions, limiter));
        env.jersey().register(new AccountResource(new Ledger(jdbi)));
    }

    /** Amounts are whole cents: refuse 10.5 and "10" instead of truncating or coercing them. */
    static void strictJson(ObjectMapper mapper) {
        mapper.disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT);
        mapper.enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES);
        mapper.coercionConfigFor(LogicalType.Integer)
            .setCoercion(CoercionInputShape.String, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
    }
}
