Saving a tenant config with a trailing slash in an upstream URL crashes the gateway reload.

Steps to reproduce:
1. Run `make run-staging`.
2. Edit `config/tenants/lumen-pay.yaml` and set the upstream to `https://lumen.example.test/`.
3. Save the file.

Expected: the reload accepts the URL and requests to `/v1/payments` keep reaching the upstream.
Actual: the gateway logs a panic and stops serving.

Environment: staging, gateway built from main on 2026-11-14.
Reported by: the onboarding team.
