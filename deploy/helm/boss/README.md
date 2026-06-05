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
  --from-literal=BOSS_WORKER_API_KEY='...'

helm upgrade --install boss deploy/helm/boss --namespace boss
```

Use `make k8s-validate` from the repository root to validate chart metadata, values, and guardrails. Use `make infra-validate` to validate the local, staging, production, and private-enterprise scaling contracts in `deploy/environments/boss-infra-profiles.yaml`. This environment does not assume Helm is installed; run `helm lint deploy/helm/boss` in release CI or on a workstation with Helm available.
