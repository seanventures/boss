package bank.web;

import jakarta.ws.rs.WebApplicationException;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.util.Map;

/** A refusal: {@code {"error": "..."}}. */
public final class Errors {

    private Errors() {}

    public static WebApplicationException of(int status, String message) {
        return new WebApplicationException(Response.status(status)
            .type(MediaType.APPLICATION_JSON_TYPE)
            .entity(Map.of("error", message))
            .build());
    }

    public static WebApplicationException accountNotFound() {
        return of(404, "account not found");
    }
}
