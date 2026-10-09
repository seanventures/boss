package bank.web;

import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.container.ContainerResponseContext;
import jakarta.ws.rs.container.ContainerResponseFilter;
import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.ext.Provider;

/** Headers on every response: the training banner, and CORS for browser clients. */
@Provider
public final class ResponseHeadersFilter implements ContainerResponseFilter {

    @Override
    public void filter(ContainerRequestContext request, ContainerResponseContext response) {
        MultivaluedMap<String, Object> h = response.getHeaders();
        h.putSingle("X-Insecure-By-Design", "training only");
        h.putSingle("Access-Control-Allow-Origin", "*");
        h.putSingle("Access-Control-Allow-Credentials", "true");
        h.putSingle("Access-Control-Allow-Headers", "Authorization, Content-Type");
    }
}
