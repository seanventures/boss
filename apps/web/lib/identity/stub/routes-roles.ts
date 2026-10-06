/**
 * Extra stub routes for the roles screens. Called by stub/server.ts after its core table, with
 * its `on(method, pattern, needs, handler)`. Helpers (stubOk, stubErr, stubEffective, …) are
 * imported from ./server — safe, because they are only called when a request is handled.
 */
import type { StubOn } from "./server";

export function registerRoleRoutes(on: StubOn): void {
  void on;
}
