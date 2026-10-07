/**
 * THE PUBLIC URL (the API's H2 fix): cookies, passkeys and every emailed link are bound to one
 * public URL — BOSS_PUBLIC_URL, or the address the install was claimed at. GET /v1/auth/options
 * reports it (`public_origin`, `public_origin_source`); the posture's "origin" check is bad while
 * none is bound, and email routes answer `409 public_origin_unset`.
 */
export const PUBLIC_URL_HOWTO =
  "Set BOSS_PUBLIC_URL to the address people open BOSS at (for example BOSS_PUBLIC_URL=https://boss.example.com) in the install's .env, then restart BOSS.";

/** The same, as a tooltip (≤ 14 words: the Setup board's copy budget). */
export const PUBLIC_URL_TIP = "Set BOSS_PUBLIC_URL in the install's .env to BOSS's address, then restart.";
