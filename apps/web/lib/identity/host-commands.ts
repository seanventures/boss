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

/** The setup-link command for THIS install. A second install on one machine runs by its own
 *  launcher's full path (the API's `claim.host_command`): plain `boss` there is the FIRST install,
 *  so it would claim the other BOSS (stranger test r2, 2026-11-17). Anything that is not a plain
 *  `.../boss` path falls back to `boss`. */
export function setupLinkCommand(hostCommand?: string | null): string {
  const cmd = (hostCommand ?? "").trim();
  if (!cmd || cmd === "boss" || !/^[A-Za-z0-9._/~+-]+\/boss$/.test(cmd)) return HOST_COMMANDS.setupLink;
  return `${cmd} identity setup-link`;
}

/** Whether this page is opened on a loopback name (localhost, 127.x, ::1): the server's own
 *  machine, or an SSH tunnel to it. Then "you're not on the server's network" is wrong. */
export function onLoopback(hostname: string | null | undefined): boolean {
  const h = (hostname ?? "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  return h === "localhost" || h.endsWith(".localhost") || /^127\.\d+\.\d+\.\d+$/.test(h) || h === "::1";
}

export const overSsh = (command: string) => `ssh -t user@your-server '${command}'`;
