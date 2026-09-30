# Put the console behind HTTPS

Serve Mission Control on a hostname you control, such as `https://console.example`, by running a TLS proxy on the install machine. The console listener itself never leaves loopback: on the install machine it stays at `http://127.0.0.1:3000` (or your `BOSS_WEB_PORT`), and remote browsers reach it only through the proxy.

This page also covers publishing the API port for remote clients.

## 1. Point a hostname at the machine

Point a DNS hostname you control at the install machine, or add a hosts entry on each computer that will open the console:

```text
203.0.113.10 console.example
```

The installer rejects loopback names such as `localhost` and `127.0.0.1`, so use a real hostname. Never put the API key in the URL.

## 2. Install with the console origin

From a checkout on the install machine:

```bash
./install.sh --dir "$PWD" --console-origin https://console.example
```

The installer normalizes the origin and writes it to `BOSS_CONSOLE_PUBLIC_ORIGIN` in the private `.env`:

- `https://console.example:443` is stored as `https://console.example`.
- A non-default port is kept, for example `https://console.example:8443`.
- The value must be a bare origin, with no path, query, userinfo, or trailing slash.

The web port, Postgres, and Infisical (if you use it) all remain bound to `127.0.0.1`.

## 3. Run a TLS proxy

Run the proxy on the install machine. This nginx server block is the minimum the console needs; replace the hostname and supply your own certificate files.

```nginx
server {
    listen 443 ssl;
    server_name console.example;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host console.example;
        proxy_set_header X-Forwarded-Host console.example;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

!!! warning "Overwrite the forwarded headers"
    The proxy must set `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto` to fixed values, as shown above. Passing through the client's values with `$http_x_forwarded_host` or `$http_x_forwarded_proto` is not enough.

If your origin uses a port other than 443, use that `host:port` in both `Host` and `X-Forwarded-Host`, and keep forwarding to the same loopback web port.

`https://console.example` answers once the proxy is running. On the install machine itself, Mission Control stays at `http://127.0.0.1:3000`. Before exposing the console, run the verification checks in [Sign in to Mission Control](CONSOLE_AUTH.md#requirements-for-remote-https).

## Add a hostname to a running install

Set the origin in the private `.env` on the install machine:

```bash
BOSS_CONSOLE_PUBLIC_ORIGIN=https://console.example
```

Then recreate the web container without rebuilding:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile web up -d --force-recreate --no-deps web
```

Later runs of `./install.sh` keep this origin unless you pass `--console-origin` or export `BOSS_CONSOLE_PUBLIC_ORIGIN`.

## Publish the API for remote clients

By default the API accepts connections only on `127.0.0.1`. To publish it for remote clients, pass `--api-bind`:

```bash
./install.sh --dir "$PWD" --api-bind 0.0.0.0
```

`--api-bind` accepts `127.0.0.1`, `0.0.0.0`, or a single IPv4 or IPv6 address. Hostnames and blank values are rejected, and IPv6 addresses are stored in brackets so the published port is unambiguous. The setting is saved as `BOSS_API_BIND` in `.env`, and Compose maps `${BOSS_API_BIND}:${BOSS_API_PORT}` to port 8000 in the container.

Publishing the port does not relax security:

- Authentication stays on. There is no switch to disable `BOSS_REQUIRE_API_AUTH`.
- The API key stays in `.env`. It is never a URL parameter, and the installer will not accept it as a command-line argument.
- Postgres stays local and should never be published. The API and worker reach it over the Compose network.
- The web listener stays on loopback even when the API bind is wider.

See [Call the API from another machine](remote-api.md) for authenticating remote calls and adding TLS.

## Local access

On the install machine, open the terminal with `make metatron`, or load CLI credentials with `source boss-community.env.sh`. To reach the console from another computer without a proxy, use an SSH tunnel as described in [Sign in to Mission Control](CONSOLE_AUTH.md).
