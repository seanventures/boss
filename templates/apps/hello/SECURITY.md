# Hello endpoint: security note

Protects nothing and stores nothing. `GET /hello` is public and returns a fixed greeting from
configuration; it reads no request data, so it has no injection surface.
