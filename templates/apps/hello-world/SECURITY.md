# Hello World: security note

Protects nothing and stores nothing. `GET /` is public and returns a fixed greeting from
configuration and the server's clock; the name is HTML-escaped and no request data is read, so it
has no injection surface.
