# Console hostname

The Mission Control listener stays on loopback. A name does not move it. On this machine the console is `http://127.0.0.1:3000` (or whatever `BOSS_WEB_PORT` is). A browser on another computer reaches it only through a TLS proxy on this machine that forwards to that loopback port.

This page is for a self-hosted install on a machine you administer.

## DNS or a hosts entry

Point a DNS hostname you control at this machine, or add a hosts entry on the computer that will open the console:

```text
203.0.113.10 console.example
```

Use a DNS hostname. Loopback names such as `localhost` and `127.0.0.1` are rejected. Do not put the API key in the URL.

## Install

From a checkout on this machine:

```bash
./install.sh --dir "$PWD" --console-origin https://console.example
```

The installer writes the normalized origin to `BOSS_CONSOLE_PUBLIC_ORIGIN` in the private `.env`. `https://console.example:443` is stored as `https://console.example`. A different port is kept, for example `https://console.example:8443`. The value is an origin: no path, query, userinfo, or trailing slash.

The web port stays bound to `127.0.0.1`. Postgres stays on `127.0.0.1`. Infisical, when you use it, stays on `127.0.0.1`.

## TLS proxy

Run the proxy on this machine. This nginx server block is the minimum that satisfies the console. Replace the hostname and use certificate files you already have. The proxy must overwrite `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`. Copying the client's values is not enough, so these lines are fixed text, not `$http_x_forwarded_host` or `$http_x_forwarded_proto`.

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

If the origin includes a port other than 443, use that `host:port` in both `Host` and `X-Forwarded-Host`, and forward to the same loopback web port.

`https://console.example` answers only after this proxy is in place. On this machine, Mission Control remains `http://127.0.0.1:3000`.

## An install that is already running

Set the origin in the private `.env` on this machine:

```bash
BOSS_CONSOLE_PUBLIC_ORIGIN=https://console.example
```

Recreate the web container without rebuilding:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env --profile web up -d --force-recreate --no-deps web
```

A later `./install.sh` keeps that origin unless this run passes `--console-origin` or exports `BOSS_CONSOLE_PUBLIC_ORIGIN`.

## Remote API clients

The API accepts connections on `127.0.0.1` unless you opt in. To publish it on this machine for remote clients:

```bash
./install.sh --dir "$PWD" --api-bind 0.0.0.0
```

`--api-bind` accepts `127.0.0.1`, `0.0.0.0`, or one IPv4 or IPv6 address. Hostnames and blank values are rejected. An IPv6 address is stored in brackets so the published port is unambiguous. The same variable is `BOSS_API_BIND` in `.env`. The compose port is `${BOSS_API_BIND}:${BOSS_API_PORT}` mapped to port 8000 in the container.

Authentication stays required. There is no switch to turn off `BOSS_REQUIRE_API_AUTH`. The API key stays in `.env`. It is not a URL parameter, and the installer will not take it as a command-line argument.

Postgres stays local. Do not publish it. The API and worker reach it on the compose network. The web listener stays on loopback even when the API bind is wider.

## On this machine

The terminal on this machine is `make metatron`. The CLI on this machine is `source boss-community.env.sh`. An SSH tunnel is the path that does not need a proxy; see [Sign in to Mission Control](CONSOLE_AUTH.md).
