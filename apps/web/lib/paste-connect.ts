/** Success copy for Community paste-and-connect. Never interpolates the raw key. */
export function pasteConnectSuccessCopy(secretRefId: string): string {
  return `Stored as secret_ref ${secretRefId}. The raw key is not kept in BOSS.`;
}
