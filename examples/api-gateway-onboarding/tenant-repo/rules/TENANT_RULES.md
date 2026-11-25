# Tenant rules

`tools/tenantlint.py` checks these. Security rules protect callers and data; guideline rules keep
the shared gateway healthy and operable.

| Rule | Kind | Severity | What it asks |
|---|---|---|---|
| <a id="no-inlined-secrets"></a>`no-inlined-secrets` | security | error | No API key, token or password in the file. Reference a secret manager entry (`api_key_ref`). |
| <a id="cors-no-wildcard-with-credentials"></a>`cors-no-wildcard-with-credentials` | security | error | `allow_origins: ["*"]` never with `allow_credentials: true`. |
| <a id="upstream-https-required"></a>`upstream-https-required` | security | error | Every upstream `url` starts with `https://`. |
| <a id="no-sensitive-paths-exposed"></a>`no-sensitive-paths-exposed` | security | error | No `/admin`, `/internal`, `/debug` or `/actuator` route. |
| <a id="rate-limit-required"></a>`rate-limit-required` | guideline | error | Every route has `rate_limit.rps` and `rate_limit.burst`. |
| <a id="request-size-capped"></a>`request-size-capped` | guideline | error | Every route has `max_body_bytes`. |
| <a id="path-versioned"></a>`path-versioned` | guideline | error | Every path starts with a version, such as `/v1/`. |
| <a id="pii-fields-declared"></a>`pii-fields-declared` | guideline | warn | A tenant with `data_classification: pii` lists `pii_fields`. |
| <a id="owner-contact-required"></a>`owner-contact-required` | guideline | warn | The file names an `owner` and a `contact`. |
