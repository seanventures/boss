package bank.api;

import bank.auth.User;
import bank.ledger.Ledger;
import io.dropwizard.auth.Auth;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.List;
import java.util.Map;

/**
 * Accounts, deposits, withdrawals, balances and transfers. Every route needs a bearer token
 * ({@code @Auth}) and passes the caller's id to the ledger, which refuses accounts that are not
 * the caller's. Path ids are {@code long}: anything else is a 404 before any SQL runs.
 */
@Path("/api")
@Produces(MediaType.APPLICATION_JSON)
@Consumes(MediaType.APPLICATION_JSON)
public final class AccountResource {

    private final Ledger ledger;

    public AccountResource(Ledger ledger) {
        this.ledger = ledger;
    }

    @POST
    @Path("/accounts")
    public Response open(@Auth User user, @Valid Dtos.OpenAccount body) {
        String currency = body == null || body.currency() == null ? "USD" : body.currency();
        return Response.status(201).entity(ledger.open(user.id(), currency)).build();
    }

    @GET
    @Path("/accounts")
    public Map<String, List<Ledger.Account>> list(@Auth User user) {
        return Map.of("accounts", ledger.list(user.id()));
    }

    @GET
    @Path("/accounts/{id}/balance")
    public Map<String, Object> balance(@Auth User user, @PathParam("id") long id) {
        Ledger.Account a = ledger.get(user.id(), id);
        return Map.of("account_id", a.id(), "currency", a.currency(), "balance_cents", a.balanceCents());
    }

    @GET
    @Path("/accounts/{id}/entries")
    public Map<String, List<Ledger.Entry>> entries(@Auth User user, @PathParam("id") long id) {
        return Map.of("entries", ledger.entries(user.id(), id));
    }

    @POST
    @Path("/accounts/{id}/deposit")
    public Response deposit(@Auth User user, @PathParam("id") long id,
                            @NotNull @Valid Dtos.Movement body) {
        Ledger.Posted p = ledger.deposit(user.id(), id, body.amountCents(), body.idempotencyKey());
        return Response.status(p.replayed() ? 200 : 201).entity(p).build();
    }

    @POST
    @Path("/accounts/{id}/withdraw")
    public Response withdraw(@Auth User user, @PathParam("id") long id,
                             @NotNull @Valid Dtos.Movement body) {
        Ledger.Posted p = ledger.withdraw(user.id(), id, body.amountCents(), body.idempotencyKey());
        return Response.status(p.replayed() ? 200 : 201).entity(p).build();
    }

    @POST
    @Path("/transfers")
    public Response transfer(@Auth User user, @NotNull @Valid Dtos.Transfer body) {
        Ledger.Transferred t = ledger.transfer(user.id(), body.fromAccountId(), body.toAccountId(),
            body.amountCents(), body.idempotencyKey());
        return Response.status(t.replayed() ? 200 : 201).entity(t).build();
    }
}
