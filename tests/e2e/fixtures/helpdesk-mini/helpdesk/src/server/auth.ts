import type { RequestHandler, Router } from "express";

// The login-oauth connector, mounted as BOSS's brief says for an app with its own router: its
// .mjs files are kept byte-for-byte and loaded with import(). The connector reads SESSION_SECRET
// itself and refuses to start without one of at least 32 characters (BOSS generates it).
export interface AuthHandle {
  router: Router;
  requireUser: RequestHandler;
  requireRole: (role: string) => RequestHandler;
}

export async function mountAuth(): Promise<AuthHandle> {
  const { loadConfig } = await import("../../auth/config.mjs");
  const { createAuth } = await import("../../auth/routes.mjs");
  return createAuth(loadConfig()) as AuthHandle;
}
