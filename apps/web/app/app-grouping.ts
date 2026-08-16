/**
 * Which runs are the same app.
 *
 * This kept getting fixed one duplicate at a time -- app_id, then root_run_id, then
 * deferred_from_run, then parent_run_id -- each a new `||` on the end of a chain, and each
 * fixing the case in front of me while leaving its siblings split. ApproveIt still appeared
 * three times: twice as separate lineages and once more in the catalogue.
 *
 * The chain was the bug. It picks the FIRST identity a run happens to carry, so two runs of
 * one app that carry different identities never meet. Identity is not a priority order, it is
 * a relation: a run may be tied to its app by several facts at once, and any ONE of them
 * shared with another run makes them the same app.
 *
 * So: collect every identity token a run carries, union them, and let the connected component
 * be the app. Adding a new kind of link later is one more token, not another `||` and another
 * round of this.
 *
 * The token that was missing entirely: a run's base_branch. Work started from
 * `boss/code/crun_x` is, by construction, a change to whatever crun_x built -- that is what
 * "change this app" DOES -- and it is the only link between an app and follow-up work started
 * as its own task.
 */

export type GroupableRun = {
  id: string;
  spec?: { base_branch?: string | null } | null;
  finalize_ctx?: Record<string, unknown> | null;
};

const BRANCH_RUN = /^boss\/code\/(crun_[a-z0-9]+)$/i;

/** Every fact that ties this run to an app. Any shared token means the same app. */
export function identityTokens(run: GroupableRun): string[] {
  const fctx = (run.finalize_ctx ?? {}) as Record<string, unknown>;
  const tokens = [`id:${run.id}`];

  const appId = fctx.app_id;
  if (typeof appId === "string" && appId) tokens.push(`app:${appId}`);

  const bench = (fctx.qa_benchmark as { id?: string } | undefined)?.id;
  if (bench) tokens.push(`qa:${bench}`);

  // Lineage a run records about itself. These are identity, not resemblance.
  for (const field of ["root_run_id", "deferred_from_run", "parent_run_id"]) {
    const value = fctx[field];
    if (typeof value === "string" && value) tokens.push(`id:${value}`);
  }

  // The branch it was built ON is the app it changes.
  const base = run.spec?.base_branch;
  const matched = typeof base === "string" ? base.match(BRANCH_RUN) : null;
  if (matched) tokens.push(`id:${matched[1]}`);

  return tokens;
}

/**
 * Group runs into apps, preserving the order in which apps first appear.
 *
 * Deliberately NOT by title: a name is not an identity, so rebuilding an app from scratch
 * must start a new card rather than folding into the old one.
 */
export function groupRunsIntoApps<T extends GroupableRun>(runs: T[]): T[][] {
  const parent = new Map<string, string>();
  const find = (x: string): string => {
    let root = x;
    while (parent.get(root) && parent.get(root) !== root) root = parent.get(root)!;
    return root;
  };
  const union = (a: string, b: string) => {
    parent.set(find(a), find(b));
  };

  for (const run of runs) {
    const tokens = identityTokens(run);
    for (const token of tokens) if (!parent.has(token)) parent.set(token, token);
    for (let i = 1; i < tokens.length; i += 1) union(tokens[0], tokens[i]);
  }

  const order: string[] = [];
  const groups = new Map<string, T[]>();
  for (const run of runs) {
    const root = find(identityTokens(run)[0]);
    if (!groups.has(root)) { groups.set(root, []); order.push(root); }
    groups.get(root)!.push(run);
  }
  return order.map((root) => groups.get(root)!);
}
