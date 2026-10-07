"use client";

import { runPressedAction } from "../app/press-relay";
import { INTENT_FIELD, type Press, runPress } from "./api-write";
import { currentMirror, wantsStepUp, wasForbidden } from "./identity/mirror";
import type { Permission } from "./identity/permissions.gen";
import { permissionName } from "./identity/words";

/**
 * THE ONE WAY CLIENT CODE INVOKES A SERVER ACTION.
 *
 * lib/api-write.ts keys every fetch the console makes. Server actions are the other door: 63 of
 * them were called from code (`await redeployAction(id)`, `runAgentActionAction(...)`, the chat
 * turn's `askBossAction(null, fd)`) rather than from a SubmitButton form, and those carried no
 * press at all — so the duplicate the four incidents describe (2026-08-22 a run started twice,
 * 08-30 "Open preview happened twice", 09-10 duplicate chat actions, 10-02 "Stop and run again"
 * pressed twice started two workflows) was still open on Deploy, Redeploy, the shepherd's sweep
 * and the chat. This closes it with the same rules as a fetch write:
 *
 *   - each call is one press with a fresh key, carried to the server by app/press-relay.ts so
 *     the action's API writes are keyed and the API runs a repeated press once;
 *   - the same call again while the first is running (a double click) joins it — one request,
 *     both callers get its result;
 *   - with `press`, a different call in the same scope is refused and the refusal is announced
 *     in the operator's words (lib/api-write.ts `runPress`).
 *
 * tests/every-write-goes-through-api-write.test.ts fails when client code references a server
 * action any other way than through this, or as the action of a form that has a SubmitButton.
 */

const actionIds = new WeakMap<object, number>();
let nextActionId = 0;
let unshareable = 0;

function idOf(action: object): number {
  let id = actionIds.get(action);
  if (id === undefined) { id = ++nextActionId; actionIds.set(action, id); }
  return id;
}

/** What "the same call" is for an action: the action and its arguments. A FormData compares by
 *  its text fields; a file or anything that cannot be written down makes the call unique. */
function fingerprintOf(action: object, args: unknown[]): string {
  try {
    const shown = JSON.stringify(args, (_key, value) => {
      if (typeof FormData !== "undefined" && value instanceof FormData) {
        const fields: [string, string][] = [];
        value.forEach((v, k) => {
          if (typeof v !== "string") throw new Error("file");
          if (k !== INTENT_FIELD) fields.push([k, v]);
        });
        return { formData: fields };
      }
      return value === undefined ? null : value;
    });
    return `action#${idOf(action)} ${shown}`;
  } catch {
    return `action#${idOf(action)} #unique-${++unshareable}`;
  }
}

/**
 * Wrap a server action so every invocation is a keyed press. `press` (fixed, or decided from the
 * arguments) is for an operator's button; leave it out for loads and background calls.
 */
export function callAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
  press?: Press | ((...args: A) => Press | undefined),
  /** The permission the action declares (lib/identity/action-permissions.ts). With it, a result
   *  that says `step_up_required` opens the step-up sheet and the call is made once more, and a
   *  `forbidden` refreshes the console's mirror so the control disables. The API decides. */
  permission?: Permission,
): (...args: A) => Promise<R> {
  const once = (...args: A) => runPress<R>({
    press: typeof press === "function" ? press(...args) : press,
    fingerprint: fingerprintOf(action, args),
  }, (key) => runPressedAction({ [INTENT_FIELD]: key } as { __boss_intent: string },
                               action, ...args));
  if (!permission) return once;
  return async (...args: A) => {
    const first = await once(...args);
    const mirror = currentMirror();
    if (mirror && wantsStepUp(first)) {
      if (await mirror.stepUp(`This needs “${permissionName(permission)}”.`)) return once(...args);
    } else if (mirror && wasForbidden(first)) {
      mirror.refresh().catch(() => undefined);
    }
    return first;
  };
}
