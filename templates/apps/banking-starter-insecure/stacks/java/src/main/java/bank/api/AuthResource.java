package bank.api;

import bank.auth.Tokens;
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

    private final Users users;
    private final Tokens tokens;

    public AuthResource(Users users, Tokens tokens) {
        this.users = users;
        this.tokens = tokens;
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
        Users.Result r = users.check(body.username(), body.password());
        switch (r.check()) {
            case NO_SUCH_USER -> throw Errors.of(404, "no such user: " + body.username());
            case WRONG_PASSWORD -> throw Errors.of(401, "wrong password");
            default -> { }
        }
        return Response.ok(Map.of("token", tokens.issue(r.user()), "token_type", "Bearer")).build();
    }

    @POST
    @Path("/logout")
    public Response logout(@Auth User user) {
        return Response.noContent().build();
    }

    @POST
    @Path("/password")
    public Response changePassword(@Auth User user, @NotNull @Valid Dtos.ChangePassword body) {
        users.setPassword(user.id(), body.newPassword());
        return Response.noContent().build();
    }
}
