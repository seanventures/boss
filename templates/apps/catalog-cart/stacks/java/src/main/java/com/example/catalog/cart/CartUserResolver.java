package com.example.catalog.cart;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Component;

/**
 * Who owns the request's cart. With login-oauth (Spring Security) applied, the signed-in
 * principal's name is the app's user id; without a login every cart is anonymous.
 */
public interface CartUserResolver {
    String userId(HttpServletRequest request);

    @Component
    class FromPrincipal implements CartUserResolver {
        @Override
        public String userId(HttpServletRequest request) {
            return request.getUserPrincipal() == null ? null : request.getUserPrincipal().getName();
        }
    }
}
