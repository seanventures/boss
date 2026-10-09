package com.example.catalog.cart;

import java.util.Map;

/** A refusal with a fixed code (never a stack trace). */
public class CatalogError extends RuntimeException {
    public final int status;
    public final String code;
    public final Map<String, Object> extra;

    public CatalogError(int status, String code) {
        this(status, code, Map.of());
    }

    public CatalogError(int status, String code, Map<String, Object> extra) {
        super(code);
        this.status = status;
        this.code = code;
        this.extra = extra;
    }
}
