/**
 * Is the identity stub API answering? Only when BOSS_IDENTITY_STUB is set to this exact,
 * deliberately un-guessable-by-accident value — never by a truthy "1". A console built for a
 * real install has no such variable (tests/identity-stub-is-never-shipped.test.ts), so the stub
 * can never stand in for real authentication there. Read on the server only.
 */
export const STUB_ENABLE_VALUE = "fixtures-not-for-production";

export function identityStubEnabled(): boolean {
  return typeof window === "undefined" && process.env.BOSS_IDENTITY_STUB === STUB_ENABLE_VALUE;
}

/** The API paths the stub answers while it is on: everything identity owns. */
export function isIdentityPath(pathname: string): boolean {
  return pathname.startsWith("/v1/identity/") || pathname === "/v1/identity" || pathname.startsWith("/v1/auth/");
}
