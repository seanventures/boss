/**
 * JOURNEYS J1–J15 (IDENTITY.md §15.2), written for the INDEPENDENT TESTER who walks each one in a
 * real browser (BOSS practice). Each step says where to go, what to do, and what must be true.
 *
 * Two ways to walk them:
 *   - CONSOLE HALF, today: a production build with the identity stub
 *       BOSS_IDENTITY_STUB=fixtures-not-for-production BOSS_API_URL=http://127.0.0.1:9 next start
 *     Fixture people (any password but "wrong"; MFA accepts any 6 digits but 000000; the stub's
 *     passkey prompt succeeds without a device): param@example.com Owner · priya@acme.dev Admin ·
 *     sam@acme.dev Operator (+Admin in acme-web) · diego@acme.dev Viewer · mei@acme.dev Auditor.
 *     First-run claims: set the cookie `boss_stub_claim` to setup_token | legacy_password |
 *     network_bridge before opening /claim. `boss_stub_bare=1` = nothing configured; `boss_stub_signup=1`.
 *   - FULL JOURNEY, once S1/S2/S3/S5 land: the same steps on a real stack (no stub variable). The
 *     `needs` list names the streams whose API half each journey also proves.
 *
 * tests/identity-journeys.test.ts checks every URL here is a real page or public path, every
 * persona exists, and runs the API-seam half of the journeys the stub can answer.
 */
export type Persona = "owner" | "admin" | "operator" | "viewer" | "auditor" | "anonymous" | "network";

export type JourneyStep = { go?: string; do: string; expect: string };

export type Journey = {
  id: `J${number}`;
  title: string;
  persona: Persona;
  /** Cookies to set before starting (stub only). */
  stub?: Record<string, string>;
  needs: ("S1" | "S2" | "S3" | "S5")[];
  steps: JourneyStep[];
};

export const PERSONA_EMAIL: Record<Exclude<Persona, "anonymous" | "network">, string> = {
  owner: "param@example.com", admin: "priya@acme.dev", operator: "sam@acme.dev", viewer: "diego@acme.dev", auditor: "mei@acme.dev",
};

export const JOURNEYS: Journey[] = [
  { id: "J1", title: "Fresh install: claim with a passkey", persona: "anonymous", stub: { boss_stub_claim: "setup_token" }, needs: ["S1", "S5"], steps: [
    { go: "/claim#t=setup-token-from-the-logs", do: "Read the panel", expect: "“Claim this BOSS”, setup link expiry in the logo row, name/email/seat fields" },
    { do: "Fill name, email, seat ceo; press Create a passkey and continue", expect: "Passkey prompt; then recovery codes shown once with Download/Print and a gated Continue" },
    { do: "Tick I've stored them; Continue", expect: "Lands on /identity Overview as Owner (top bar pill “Param · Owner”)" },
    { go: "/claim", do: "Open the claim again", expect: "“Already claimed” (API: setup endpoint 404 for good)" },
  ] },
  { id: "J2", title: "Upgrade with the shared console password", persona: "anonymous", stub: { boss_stub_claim: "legacy_password" }, needs: ["S1", "S5"], steps: [
    { go: "/login", do: "Read the sign-in panel", expect: "A “This BOSS has no Owner yet. Claim it” door above the providers" },
    { go: "/claim", do: "Open “Where's my password?”", expect: "The two commands with Copy; the password is never shown" },
    { do: "Enter the shared password, name, email, seat; claim; enrol a passkey", expect: "Owner session; banner with the legacy-password countdown (S5)" },
  ] },
  { id: "J3", title: "Invite an Operator, accept, enrol TOTP, step-up", persona: "owner", needs: ["S1"], steps: [
    { go: "/identity/people", do: "Invite people → ana@acme.dev, Operator", expect: "Roles you do not fully hold are disabled with a reason; sent toast, or one link per person without SMTP" },
    { go: "/invite/inv_test", do: "As the invitee: name + password, Accept", expect: "Goes to /enrol" },
    { go: "/enrol", do: "Choose Authenticator app; enter a code", expect: "Secret shown once; recovery codes shown once; Continue gated" },
    { go: "/identity/roles/arl_release_captain", do: "Toggle a permission; Review and save", expect: "Step-up sheet: passkey first, then the 6-digit code" },
  ] },
  { id: "J4", title: "Forgot password with SMTP", persona: "anonymous", needs: ["S1"], steps: [
    { go: "/login", do: "Forgot password? → enter any email", expect: "Uniform “If an account exists…” words, whatever the address" },
    { go: "/reset#t=reset-token", do: "Set a 12+ character password", expect: "Then asked for the second factor; other sessions revoked (API)" },
  ] },
  { id: "J5", title: "Lost MFA: recovery code; admin reset", persona: "operator", needs: ["S1"], steps: [
    { go: "/login", do: "sam@acme.dev + password → Use a recovery code", expect: "Recovery code field; signs in; below 3 left forces regeneration (API)" },
    { go: "/identity/people/usr_theo", do: "As Owner: Reset MFA", expect: "Step-up, then a confirmation; for an Owner target the API needs owner:assign" },
  ] },
  { id: "J6", title: "Sessions on two browsers", persona: "operator", needs: ["S1"], steps: [
    { go: "/account/sessions", do: "End the other session", expect: "It disappears; that browser is sent to /login on its next request" },
  ] },
  { id: "J7", title: "OIDC sign-in, group sync, SCIM disable", persona: "anonymous", needs: ["S1", "S2"], steps: [
    { go: "/login", do: "Press Acme SSO", expect: "Navigates to /v1/auth/sso/idp_okta/start (IdP redirect)" },
    { go: "/identity/groups/grp_builders", do: "Inspect the Okta group", expect: "Source badge Okta; members read-only; role bindings listed" },
  ] },
  { id: "J8", title: "JIT consent for a build, then the lineage", persona: "operator", needs: ["S3"], steps: [
    { do: "Start a build whose plan needs more than standing grants (S3 answers 409 consent_required)", expect: "Consent sheet: “Let Forge and Vera act for you?”, chips, For this build only / For 30 days" },
    { go: "/identity/agents/delegations/dlg_01JB7Q", do: "Read the chain", expect: "Sam → Grant → Atlas → Forge → Vera, chips narrowing left to right, dropped ones struck" },
  ] },
  { id: "J9", title: "Revoke mid-run: the cascade", persona: "owner", needs: ["S3"], steps: [
    { go: "/identity/agents/delegations/dlg_01JB7Q", do: "Revoke grant", expect: "Cascade preview: halts runs, revokes tokens, GitHub token, child grant, pauses a schedule" },
    { do: "Revoke and halt", expect: "Status revoked; audit shows delegation.revoked" },
  ] },
  { id: "J10", title: "Lower Forge's ceiling", persona: "admin", needs: ["S3"], steps: [
    { go: "/identity/agents/agt_forge", do: "Turn off preview:manage; save (step-up)", expect: "Lineage gains ceiling_changed −preview:manage; non-delegable permissions are locked" },
  ] },
  { id: "J11", title: "The Viewer walk", persona: "viewer", needs: ["S2"], steps: [
    { go: "/", do: "Read the rail", expect: "Only areas the Viewer can open; Identity present (directory:read)" },
    { go: "/identity", do: "Click every enabled control", expect: "No mutating request returns 2xx; Invite disabled with “Needs “Invite people” · ask an Admin”" },
    { go: "/identity/people", do: "Open directly", expect: "AccessDenied: “You can't open People”, Needs “View people”, Admins named, Request access" },
  ] },
  { id: "J12", title: "Role editor: clone Operator, remove deployment:create", persona: "admin", needs: ["S2"], steps: [
    { go: "/identity/roles", do: "Clone Operator", expect: "A custom copy opens in the editor" },
    { do: "Turn off deployment:create", expect: "Changes shows −“Create deployments” (deployment:create in its tooltip); Impact preview lists people, pages and route counts" },
    { do: "Save (step-up)", expect: "Version +1; an affected Deploy button disables live after the epoch moves" },
  ] },
  { id: "J13", title: "Service account key: shown once, rotate with overlap", persona: "admin", needs: ["S1"], steps: [
    { go: "/identity/service-accounts?sa=sva_ci_deployer", do: "New key → CI deploy preset (scopes “Create deployments”, “View deployments”, “View builds”) → Create key", expect: "Shown-once dialog; Done disabled until “I've stored this key” is ticked; Escape does not close" },
    { do: "Close; read the keys table", expect: "Only the key id (key_…), never any part of the secret" },
    { do: "Rotate key_7f3k2q with 1 h overlap", expect: "Old key shows grace; both visible during the overlap" },
  ] },
  { id: "J14", title: "Network mode (goldeneye rehearsal)", persona: "network", stub: { boss_stub_claim: "network_bridge" }, needs: ["S1", "S5"], steps: [
    { go: "/claim", do: "Read the origin", expect: "“This BOSS will be bound to https://goldeneye…ts.net”; confirm required; passkey required" },
    { do: "Claim; then Sign-in & SSO → Require sign-in (step-up)", expect: "Network principal disabled; its history keeps its name (API)" },
  ] },
  { id: "J15", title: "Audit: filter by grant, drawer, export, verify", persona: "auditor", needs: ["S5"], steps: [
    { go: "/identity/audit?grant=dlg_01JB7Q", do: "Open the authz.denied row", expect: "Drawer: Sam → Atlas → Forge chain, why rows, redacted JSON, integrity hashes" },
    { do: "Export · step-up; Verify now", expect: "Step-up sheet before export; “Chain verified” pill updates" },
  ] },
];
