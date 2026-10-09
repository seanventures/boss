package bank.api;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.io.IOException;
import java.io.InputStream;
import java.util.Map;

/** Public, read-only: a landing answer, a health check, and the OpenAPI document. */
@Path("/")
@Produces(MediaType.APPLICATION_JSON)
public final class MetaResource {

    private final byte[] openapi;

    public MetaResource() {
        try (InputStream in = MetaResource.class.getResourceAsStream("/openapi.json")) {
            openapi = in == null ? "{}".getBytes() : in.readAllBytes();
        } catch (IOException e) {
            throw new IllegalStateException("openapi.json is unreadable", e);
        }
    }

    @GET
    public Map<String, String> root() {
        return Map.of("name", "banking-starter", "openapi", "/openapi.json");
    }

    @GET
    @Path("health")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }

    @GET
    @Path("openapi.json")
    public Response openapi() {
        return Response.ok(openapi, MediaType.APPLICATION_JSON_TYPE).build();
    }
}
