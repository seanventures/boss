# BOSS Community 1.0.0-rc.10.35 candidate

RC10.35 makes BOSS much smaller on disk, fixes a sign-in hang, and makes failures say what to do. It is the build for enterprise design-partner testing.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**BOSS uses far less disk**

- Images are smaller:

  | Image | Before | Now |
  |---|---|---|
  | egress | 6.8 GB | 0.96 GB |
  | mcp-tools | 6.8 GB | 1.86 GB |
  | api / worker | 6.8 GB each | 5.5 GB, sharing their layers |

  Only the images that run coding agents carry the toolchains and browser.
- A first install now fits on a 30 GB machine. The installer builds each image once and clears its build cache between groups on small disks. It asks for 21 GB free and says so before it starts.
- Each image has a size budget, and the build fails, naming the image, if one grows past it.
- A running install no longer grows without limit:
  - Old rollback images are removed 72 hours after a healthy upgrade.
  - Customer installs never create test databases, and leftovers are removed.
  - Scheduled backups are capped at 12 or 5 GB; the newest 3 are always kept. A production install asks before deleting.
  - Tables that only grow now expire old rows and are compacted.
  - Unchanged vectors are no longer rewritten.
  - Sandboxes from runs that ended are cleaned up after 24 hours.
  - Containers are removed together with their volumes.
- **`boss doctor`** and **Govern › Storage** show disk use by category and warn at 80% of each budget. `docs/operations/DISK_BUDGET.md` gives the numbers: about 11 GB fresh, 20–30 GB in daily use, plus about 7 GB of rollback images for 72 hours after an upgrade.

**Sign-in no longer hangs while agents work**

- Before, a background agent step could hold a database lock for its whole run, and every sign-in (password or passkey) waited behind it. Sign-in then showed "BOSS can't be reached" or kept waiting for the device.
- Background work now waits at most 30 seconds for a lock and is ended after 5 idle minutes. It writes its audit events only when it saves, so sign-ins never queue behind it.
- A step whose database transaction is lost three times in a row now stops with a plain reason instead of retrying forever.

**Failures say what happened and what to do**

- Failure messages across the engine, installer and release scripts now state what happened and the next step. A test enforces this for new messages.
- Lists of known test failures can only shrink.

**Enterprise networks**

- An enterprise test lane runs BOSS on a small locked-down Linux box: 4 vCPU, 8 GB, AppArmor, all traffic through an inspecting proxy with its own CA, and private package mirrors. It now passes on a re-run as well as on a fresh box.
- The installer checks the apt mirror before it starts.
- A host that the proxy blocks gets a refusal naming the egress allow setting.
- The build cache is capped.
- Time caps count from when work actually starts.
- A project with several apps never builds on an empty main branch.
- A default branch that was renamed is followed.
- The MCP gateway token renews from each session's start.

**Approval gates you set to manual stay with a person**

- A workflow approval set to `approval: {mode: manual}` is now never answered by development autonomy.

## Upgrade notes

- Run `boss upgrade` or `remote.sh update` as usual.
- To reclaim space already used by an older install, on a host that runs only BOSS:

  ```
  docker builder prune -f
  docker images --format '{{.Repository}}:{{.Tag}}' | grep ':prev-boss-community' | xargs -r docker image rm
  docker volume prune -f
  ```

  The second line removes the old rollback images, so run it once the new version is working.

## Known issues

- Some background work (threads a step starts, other worker sweeps) does not yet use the new lock and idle limits: BOSSAPP-1462 and 1463.
- CI does not yet run the scripts and worker test suites; that change needs a workflow-scoped token.
- In the Storage card, a command inside a warning shows its backticks.
- The rc10.34 known issues still apply.

## Checks

- Every PR had an independent review, and every serious finding was fixed before merging. Every PR passed the local merge gate.
- End-to-end gates on the laptop install: plumbing 20 of 20, engines 21 of 21.
- Enterprise lane on a 30 GB, 4 vCPU, 8 GB AppArmor box behind an inspecting proxy, on the final image code: **all 9 steps pass**. The install takes 9 minutes and leaves 11 GB free; plumbing 20 of 20, engines 21 of 21, scripted build 3 of 3. The lane found three build faults before this release, all fixed: a pin lost when two requirement files shared a name, pip timing out behind the proxy, and the lane's own cleanup.
- The laptop install was walked in a browser (Setup, Govern › Autonomy, Govern › Storage, Work) at 1440, 1024, 768 and 400 px, with no horizontal scroll and no page errors.

Thanks to an enterprise contributor whose network fixes shipped in rc10.32 and are exercised by the new lane.
