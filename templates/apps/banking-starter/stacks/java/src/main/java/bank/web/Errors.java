package bank.web;

import jakarta.ws.rs.WebApplicationException;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.Map;

/** A refusal with a short, fixed message: {@code {"error": "..."}}. Never echoes internals. */
public final class Errors {

    private Errors() {}

    public static WebApplicationException of(int status, String message) {
        return new WebApplicationException(Response.status(status)
            .type(MediaType.APPLICATION_JSON_TYPE)
            .entity(Map.of("error", message))
            .build());
    }

    /** The same answer for "no such account" and "not your account": no existence oracle. */
    public static WebApplicationException accountNotFound() {
        return of(404, "account not found");
    }
}
