package bank.web;

import jakarta.ws.rs.WebApplicationException;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import jakarta.ws.rs.ext.ExceptionMapper;
import jakarta.ws.rs.ext.Provider;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.util.Map;

/** Unexpected errors, with the details a developer needs to debug them. */
@Provider
public final class ErrorMapper implements ExceptionMapper<RuntimeException> {

    @Override
    public Response toResponse(RuntimeException e) {
        if (e instanceof WebApplicationException wae) {
            return wae.getResponse();
        }
        StringWriter trace = new StringWriter();
        e.printStackTrace(new PrintWriter(trace));
        return Response.serverError().type(MediaType.APPLICATION_JSON_TYPE)
            .entity(Map.of("error", String.valueOf(e.getMessage()), "trace", trace.toString()))
            .build();
    }
}
