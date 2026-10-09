package com.example.login.auth;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/** A row of {@code users} (schema.sql), with the providers linked to it. */
public class AppUser {
    public final String id;
    public final String email;
    public volatile boolean emailVerified;
    public volatile String name;
    public volatile String avatar;
    public final Instant createdAt = Instant.now();
    public final List<String> providers = new ArrayList<>();

    AppUser(String id, String email, boolean emailVerified) {
        this.id = id;
        this.email = email;
        this.emailVerified = emailVerified;
    }
}
