# Northgate API Gateway + gwlint

Northgate is a reverse-proxy API gateway for multi-tenant APIs, plus `gwlint`,
a CLI that validates tenant onboarding YAML against a security/style
rulebook before it ever reaches the gateway. Both are static Go binaries
built from this one module.

> Note: there is an unrelated OSS project also named `gwlint` (sdOps/gwlint)
> that lints Kubernetes Gateway API / E