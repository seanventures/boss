/**
 * THE HOST COMMANDS THE CONSOLE TELLS SOMEONE TO RUN, in one place. Each is the `boss` command the
 * installer puts on PATH (scripts/install/boss.sh), never a container name or a script path that
 * differs per install: the claim page used to say `docker exec boss-api boss identity setup-link`,
 * which names a container no install has. tests/host-commands-the-console-shows-exist.test.ts
 * checks every one against boss.sh and the identity CLI.
 */
export const HOST_COMMANDS = {
  /** A one-hour, single-use link that claims an install with no Owner yet. */
  setupLink: "boss identity setup-link",
  /** The shared console password (installs from before named accounts). */
  password: "boss password",
  /** A locked-out Owner gets back in. */
  recoverOwner: "boss identity recover-owner --email you@example.com",
} as const;

export const overSsh = (command: string) => `ssh -t user@your-server '${command}'`;
