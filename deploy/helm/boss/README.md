# BOSS Helm Chart

This chart packages the BOSS Enterprise self-hosted control plane.

The default values intentionally fail closed:

- `auth.requireApiAuth` is true.
- `secrets.create` is false, so Helm does not install placeholder credentials.
- Provider writes, real email sends, Slack sends, sandbox merge, and sandbox deploy are disabled.
- API/web/worker have HPA and PDB guardrails by default.

Install only after creating a real Secret:

```sh
kubectl create namespace boss
kubectl create secret generic boss-secrets \
  --namespace boss \
  --from-literal=DATABASE_URL='postgresql+psycopg://...' \
  --from-literal=BOSS_API_KEYS='...' \
  --from-literal=BOSS_API_KEY_PRINCIPALS='...' \
  --from-literal=BOSS_API_KEY='...' \
  --from-literal=BOSS_WORKER_API_KEY='...' \
  --from-file=BOSS_CONSOLE_PASSWORD=/secure/path/console-password \
  --from-file=BOSS_CONSOLE_SESSION_SECRET=/secure/path/console-session-secret

helm upgrade --install boss deploy/helm/boss --namespace boss \
  --set-string web.publicOrigin=https://console.example.com
```

Create the two private console files with independent random values of at least
32 characters each and no trailing newline. The console stays unavailable to
remote browsers until `web.publicOrigin` is the exact browser-facing HTTPS
origin. Terminate TLS at a trusted ingress that
overwrites `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto` with that origin's
host, host, and `https`. Keep the web and API services private; do not expose
their HTTP ports directly. The default NetworkPolicy allows BOSS pods only; a
deployment with an ingress needs a narrow policy overlay permitting the trusted
ingress controller to reach the web pod. The web readiness probe requires an
unauthenticated `/v1/projects` request to return 401, proving the session gate
is configured.
See `docs/community/CONSOLE_AUTH.md` for login and proxy verification.

Use `make k8s-validate` from the repository root to validate chart metadata, values, and guardrails. Use `make infra-validate` to validate the local, staging, production, and private-enterprise scaling contracts in `deploy/environments/boss-infra-profiles.yaml`. This environment does not assume Helm is installed; run `helm lint deploy/helm/boss` in release CI or on a workstation with Helm available.
