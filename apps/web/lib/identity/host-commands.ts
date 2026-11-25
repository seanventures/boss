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

/** THIS install's `boss` command. A second install on one machine runs by its own launcher's full
 *  path (the API's `host_command`): plain `boss` there is the FIRST install, so a command printed
 *  with it acts on the other BOSS (stranger tests r2 and r3). Anything that is not a plain
 *  `.../boss` path falls back to `boss`. */
export function ownBoss(hostCommand?: string | null): string {
  const cmd = (hostCommand ?? "").trim();
  if (!cmd || cmd === "boss" || !/^[A-Za-z0-9._/~+-]+\/boss$/.test(cmd)) return "boss";
  return cmd;
}

/** The setup-link command for THIS install (`claim.host_command`). */
export function setupLinkCommand(hostCommand?: string | null): string {
  return HOST_COMMANDS.setupLink.replace(/^boss\b/, ownBoss(hostCommand));
}

/** A locked-out Owner's way back in, with THIS install's command (`host_command` in auth options). */
export function recoverOwnerCommand(hostCommand?: string | null): string {
  return HOST_COMMANDS.recoverOwner.replace(/^boss\b/, ownBoss(hostCommand));
}

/** The recover-owner step for the sign-in page. `/v1/auth/options` is public, so after the claim a
 *  second install's launcher path is withheld (host_command null): the page then says to run the
 *  subcommand with "this install's boss command", never a path and never a plain `boss` that would
 *  run another install. */
export function recoverOwnerStep(hostCommand: string | null | undefined): { command: string; withOwnBoss: boolean } {
  if (hostCommand === null) return { command: HOST_COMMANDS.recoverOwner.replace(/^boss\s+/, ""), withOwnBoss: true };
  return { command: recoverOwnerCommand(hostCommand), withOwnBoss: false };
}

/** "the 6-digit code for BOSS, Acme": the org's name once, never "BOSS, BOSS" when the org is
 *  still called BOSS (stranger test r3). */
export function authenticatorLabel(orgName?: string | null): string {
  const org = (orgName ?? "").trim();
  return !org || org.toLowerCase() === "boss" ? "BOSS" : `BOSS, ${org}`;
}

/** Whether this page is opened on a loopback name (localhost, 127.x, ::1): the server's own
 *  machine, or an SSH tunnel to it. Then "you're not on the server's network" is wrong. */
export function onLoopback(hostname: string | null | undefined): boolean {
  const h = (hostname ?? "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  return h === "localhost" || h.endsWith(".localhost") || /^127\.\d+\.\d+\.\d+$/.test(h) || h === "::1";
}

export const overSsh = (command: string) => `ssh -t user@your-server '${command}'`;
