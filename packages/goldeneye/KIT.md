# Goldeneye infra kit — portable system inventory

What a single bare-metal box needs to become a self-hosting PaaS: agent-deployable
apps, automatic TLS, private networking, backups that are *proven* restorable,
and enough self-healing that a human is not the control loop.

**Status:** open kit (parameterized). Live dogfood may differ in package versions;
`site.yaml` holds site-specific values. Lessons in §8 are load-bearing.

## 0. Design principles

1. **Tailnet by default, public by exception.** Admin UIs and inter-node traffic are
   tailnet-only; only end-user apps get a public hostname.
2. **One canonical DNS A record per node (or fleet apex).** Every other public name
   is a CNAME. Hardware/IP moves stay a one-record edit.
3. **Name things, never address them.** Registry, certs, and peers use DNS.
4. **Agents are first-class operators.** Ops handshake + secrets API must let an
   agent rediscover credentials and deploy path without a human.
5. **Prove it, don't assert it.** Restore drills, exposure audits, infra-drift.

## 1. Layer 0 — host baseline

| Concern | Implementation |
|---|---|
| OS | Ubuntu 24.04 LTS (primary target) |
| Storage | Fast durable volume for Docker data-root, `/srv`, backups |
| Private net | Tailscale (admin plane + inter-node) |
| Firewall | ufw default-deny; SSH restricted to tailnet + operator CIDRs |
| SSH | key-only, keepalives |
| Patching | unattended-upgrades (security origins) |
| Kernel | raised inotify limits for Traefik/compose |

## 2. Layer 1 — platform core

| Component | Role |
|---|---|
| Docker + Compose | runtime; custom `default-address-pools` |
| platform-dns | split-DNS for containers (public vs local domain) |
| Traefik | edge; public + tailnet entrypoints; LE + optional DNS-01 |
| registry | private images by DNS name |
| hubcache | Docker Hub pull-through |
| crowdsec | behavioural edge protection (optional) |
| ops-agent | agent-facing API: handshake, secrets, privileged ops |

## 3. Layer 2 — control plane (scheduled)

health-check · functional-check · appdog/autoheal · boot-heal · deploy-doctor ·
exposure-audit · infra-drift · image GC · scheduled-reboot

## 4. Layer 3 — data & backup

pg-dump (content-verified) · volume/config backup · restic local · peer mirror ·
off-site curated · **weekly restore-drill** (must fail closed if unverifiable)

## 5. Layer 4 — observability

Synthetic uptime · host metrics · logs · errors · analytics · portal · docs

## 6. Optional services

Identity · secrets vault · automation · object storage · search · e-sign ·
LLM gateway · project tracking · TURN · outbound mail only · maps

## 7. Parameterize before any install

- Domains (public apex, apps domain, local admin domain)
- Node identity (name, public IP, tailnet IP)
- Operator CIDRs
- ACME email / Cloudflare token (if DNS-01)
- Alert webhook
- BOSS API URL + project (if control-plane linked)

## 8. Lessons worth shipping

1. Size checks are not integrity checks — verify dump *content*.
2. A backup never restored is a hypothesis — drills must fail closed.
3. Committed ≠ deployed — run infra-drift daily.
4. Unversioned automation is invisible until rebuild — keep everything in git.
5. Whitelists are fleet-wide state — update every peer on IP move.
6. Registry tag delete ≠ reclaim — prune orphaned revisions carefully.
7. Measure the link with the right tool (iperf3), not single-stream `dd|ssh`.
8. Container churn can blip Tailscale — never auto-restart tailscaled via needrestart.
9. Split-horizon DNS breaks ACME DNS-01 — pin public resolvers for validation.
10. Scope agent tools to explicit allowlists — no wildcards in message relays.

## 9. Bootstrap order

```
0 host → 1 runtime → 2 edge → 3 control → 4 data → 5 observe → 6 apps
```

## 10. Portability traps

- `docker0` is not always `172.17.0.1` once address pools are set — derive at config time.
- External compose networks must pre-exist (`web`, `storage`, `ops`).
- Docker daemon uses the **host** resolver for registry names, not platform-dns.
- Verify cutovers from a **third** host (neither old nor new origin).
