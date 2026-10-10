/**
 * The "Add BOSS's key to the server" command, with the person's own key file.
 *
 * The command BOSS shows is `ssh … root@host "…"`, which assumes the laptop's default SSH key
 * opens the server. When it does not, the push fails with "Permission denied (publickey)" —
 * indistinguishable from BOSS's key being missing. An optional key file puts `-i <path>` in the
 * command shown (replacing one already there), so what is copied is what works.
 */
export function withKeyFile(command: string, keyFile: string): string {
  const path = keyFile.trim();
  if (!path || !/^ssh\s/.test(command)) return command;
  const quoted = /^[A-Za-z0-9_./~+-]+$/.test(path) ? path : `'${path.replace(/'/g, `'\\''`)}'`;
  const rest = command.replace(/^ssh\s+/, "").replace(/^-i\s+('(?:[^']|'\\'')*'|\S+)\s+/, "");
  return `ssh -i ${quoted} ${rest}`;
}
