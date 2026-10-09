package bank.api;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.io.IOException;
import java.io.InputStream;
import java.util.Map;
import org.apache.commons.text.StringEscapeUtils;

/** Public, read-only: the landing page (with the training banner), health, OpenAPI. */
@Path("/")
public final class MetaResource {

    static final String BANNER = "Insecure by design — training only";

    private final byte[] openapi;

    public MetaResource() {
        try (InputStream in = MetaResource.class.getResourceAsStream("/openapi.json")) {
            openapi = in == null ? "{}".getBytes() : in.readAllBytes();
        } catch (IOException e) {
            throw new IllegalStateException("openapi.json is unreadable", e);
        }
    }

    @GET
    @Produces(MediaType.TEXT_HTML)
    public String root() {
        return "<!doctype html><html><head><meta charset=\"utf-8\"><title>Banking starter (training)</title>"
            + "</head><body style=\"margin:0;font-family:sans-serif\">"
            + "<div role=\"alert\" style=\"background:#b00020;color:#fff;padding:12px 16px;font-weight:bold\">"
            + StringEscapeUtils.escapeHtml4(BANNER) + "</div>"
            + "<main style=\"padding:16px\"><h1>Banking starter (training variant)</h1>"
            + "<p>A deliberately insecure API for practising security review in a BOSS preview. "
            + "Never deploy it. API description: <a href=\"/openapi.json\">/openapi.json</a>.</p></main>"
            + "</body></html>";
    }

    @GET
    @Path("health")
    @Produces(MediaType.APPLICATION_JSON)
    public Map<String, String> health() {
        return Map.of("status", "ok", "notice", BANNER);
    }

    @GET
    @Path("openapi.json")
    public Response openapi() {
        return Response.ok(openapi, MediaType.APPLICATION_JSON_TYPE).build();
    }
}
