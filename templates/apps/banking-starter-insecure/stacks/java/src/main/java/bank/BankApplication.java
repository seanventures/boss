package bank;

import bank.api.AccountResource;
import bank.api.AuthResource;
import bank.api.MetaResource;
import bank.auth.TokenAuthenticator;
import bank.auth.Tokens;
import bank.auth.User;
import bank.auth.Users;
import bank.db.Database;
import bank.ledger.Ledger;
import bank.web.ErrorMapper;
import bank.web.ResponseHeadersFilter;
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
import org.jdbi.v3.core.Jdbi;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The banking starter's TRAINING variant (label insecure_by_design): previews only, never a
 * host. {@code java -jar target/banking-starter-insecure-1.0.0.jar server config.yml}.
 */
public class BankApplication extends Application<BankConfiguration> {

    private static final Logger LOG = LoggerFactory.getLogger(BankApplication.class);

    public static void main(String[] args) throws Exception {
        new BankApplication().run(args);
    }

    @Override
    public String getName() {
        return "banking-starter-insecure";
    }

    @Override
    public void initialize(Bootstrap<BankConfiguration> bootstrap) {
        bootstrap.setConfigurationSourceProvider(new SubstitutingSourceProvider(
            bootstrap.getConfigurationSourceProvider(), new EnvironmentVariableSubstitutor(false)));
    }

    @Override
    public void run(BankConfiguration config, Environment env) {
        strictJson(env.getObjectMapper());
        LOG.warn("banking-starter-insecure: insecure by design, for training in a private preview only");

        Jdbi jdbi = Database.open(config.getDatabasePath());
        Users users = new Users(jdbi);
        Tokens tokens = new Tokens(env.getObjectMapper());

        int seeded = TestUsers.seed(System.getenv("BOSS_TEST_USERS"), config.getAppEnv(), users,
            env.getObjectMapper());
        if (seeded > 0) {
            LOG.info("seeded {} test user(s) from BOSS_TEST_USERS", seeded);
        }

        env.jersey().register(new AuthDynamicFeature(new OAuthCredentialAuthFilter.Builder<User>()
            .setAuthenticator(new TokenAuthenticator(tokens, users))
            .setPrefix("Bearer")
            .setRealm("bank")
            .buildAuthFilter()));
        env.jersey().register(new AuthValueFactoryProvider.Binder<>(User.class));
        env.jersey().register(new ResponseHeadersFilter());
        env.jersey().register(new ErrorMapper());
        env.jersey().register(new MetaResource());
        env.jersey().register(new AuthResource(users, tokens));
        env.jersey().register(new AccountResource(new Ledger(jdbi)));
    }

    static void strictJson(ObjectMapper mapper) {
        mapper.disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT);
        mapper.coercionConfigFor(LogicalType.Integer)
            .setCoercion(CoercionInputShape.String, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
    }
}
