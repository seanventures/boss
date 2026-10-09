package bank.web;

import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.container.ContainerResponseContext;
import jakarta.ws.rs.container.ContainerResponseFilter;
import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.ext.Provider;

/**
 * Security headers on every response. The API serves JSON only, so the CSP allows nothing to
 * load; responses are never cached (they carry balances). No CORS headers are sent: the API is
 * same-origin only.
 */
@Provider
public final class SecurityHeadersFilter implements ContainerResponseFilter {

    @Override
    public void filter(ContainerRequestContext request, ContainerResponseContext response) {
        MultivaluedMap<String, Object> h = response.getHeaders();
        h.putSingle("X-Content-Type-Options", "nosniff");
        h.putSingle("X-Frame-Options", "DENY");
        h.putSingle("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'");
        h.putSingle("Referrer-Policy", "no-referrer");
        h.putSingle("Cache-Control", "no-store");
        h.putSingle("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
        h.putSingle("Cross-Origin-Resource-Policy", "same-origin");
    }
}
