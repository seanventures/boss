package bank.api;

import bank.auth.LoginRateLimiter;
import bank.auth.Sessions;
import bank.auth.User;
import bank.auth.Users;
import bank.web.Errors;
import io.dropwizard.auth.Auth;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.Map;

/** Register, log in, log out, change password. */
@Path("/api")
@Produces(MediaType.APPLICATION_JSON)
@Consumes(MediaType.APPLICATION_JSON)
public final class AuthResource {

    /** One message for an unknown username and a wrong password (no username enumeration). */
    static final String BAD_LOGIN = "invalid username or password";

    private final Users users;
    private final Sessions sessions;
    private final LoginRateLimiter limiter;

    public AuthResource(Users users, Sessions sessions, LoginRateLimiter limiter) {
        this.users = users;
        this.sessions = sessions;
        this.limiter = limiter;
    }

    @POST
    @Path("/register")
    public Response register(@NotNull @Valid Dtos.Register body) {
        long id = users.create(body.username(), body.password())
            .orElseThrow(() -> Errors.of(409, "username is not available"));
        return Response.status(201).entity(Map.of("id", id, "username", body.username())).build();
    }

    @POST
    @Path("/login")
    public Response login(@NotNull @Valid Dtos.Login body) {
        long wait = limiter.retryAfterSeconds(body.username());
        if (wait > 0) {
            return Response.status(429).header("Retry-After", wait)
                .entity(Map.of("error", "too many failed logins; try again later")).build();
        }
        User user = users.check(body.username(), body.password()).orElse(null);
        if (user == null) {
            limiter.recordFailure(body.username());
            throw Errors.of(401, BAD_LOGIN);
        }
        limiter.reset(body.username());
        Sessions.Issued issued = sessions.issue(user.id());
        return Response.ok(Map.of("token", issued.token(), "token_type", "Bearer",
            "expires_at", issued.expiresAt())).build();
    }

    @POST
    @Path("/logout")
    public Response logout(@Auth User user) {
        sessions.revoke(user.tokenHash());
        return Response.noContent().build();
    }

    /** Needs the current password; on success every other session of the user is revoked. */
    @POST
    @Path("/password")
    public Response changePassword(@Auth User user, @NotNull @Valid Dtos.ChangePassword body) {
        if (!users.passwordMatches(user.id(), body.currentPassword())) {
            throw Errors.of(403, "current password is incorrect");
        }
        users.setPassword(user.id(), body.newPassword());
        sessions.revokeOthers(user.id(), user.tokenHash());
        return Response.noContent().build();
    }
}
