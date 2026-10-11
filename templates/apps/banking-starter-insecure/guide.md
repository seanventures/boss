# Banking starter (training variant): setup

**Insecure by design, training only.** This app runs in BOSS's private preview and can never
be deployed to a host. Do not put real data or real passwords in it.

1. Open the preview: {{preview_url}} shows the red training banner.
2. The API is described at {{preview_url}}/openapi.json.
3. Create a user (`POST /api/register`), log in (`POST /api/login`), open an account
   (`POST /api/accounts`) and move money as in the secure banking starter.
4. Run BOSS's security review on the app. Its report is scored against the template's
   answer key (`vulns.yaml`), which the build never sees.
