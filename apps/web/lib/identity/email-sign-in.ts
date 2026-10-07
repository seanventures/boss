/**
 * Is email sign-in (magic link, email code, an emailed reset link) offered? Shared by the sign-in
 * page (a client component) and the /reset page (a server component), so it lives in a plain
 * module: a function exported from a "use client" file cannot be called on the server, and /reset
 * crashed doing exactly that (round 2, R2-WEB; tests/a-server-module-calls-no-client-function.test.ts).
 *
 * Magic link and email code send an email whose link names BOSS's public URL. Without SMTP there is
 * no email, and without a public URL (H2: `public_origin` null) the API refuses them
 * `409 public_origin_unset`, so neither is offered. An API that does not report the URL is trusted.
 */
import type { AuthOptions } from "./types";

export function emailSignInOffered(options: Pick<AuthOptions, "smtp" | "public_origin">): boolean {
  return options.smtp && options.public_origin !== null;
}
