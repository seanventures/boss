"use client";

import { runPressedAction } from "../app/press-relay";
import { INTENT_FIELD, type Press, runPress } from "./api-write";
import { noteDecisionResult } from "./decided-gates";
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
 * The arguments as THIS press sends them: a FormData argument carries the press's own key in its
 * `__boss_intent` field. A form's SubmitButton put its key there, and that key stays the same
 * while the press is pending — so the re-send after a step-up carried the refused press's key,
 * middleware.ts lifted it (it is the first `__boss_intent` in the body), and the API replayed
 * the stored refusal (BOSSAPP-510). Every press now names itself, so a re-send is a new press.
 */
function argsForPress<A extends unknown[]>(args: A, key: string): A {
  if (typeof FormData === "undefined") return args;
  return args.map((arg) => {
    if (!(arg instanceof FormData) || !arg.has(INTENT_FIELD)) return arg;
    const copy = new FormData();
    arg.forEach((v, k) => { if (k !== INTENT_FIELD) copy.append(k, v); });
    copy.set(INTENT_FIELD, key);
    return copy;
  }) as A;
}

/** Whether a call's answer — a result, or a thrown refusal carrying `identity` — asks for a fresh
 *  second factor. */
function askedForStepUp(outcome: { value?: unknown; error?: unknown }): boolean {
  if ("error" in outcome) {
    const identity = (outcome.error as { identity?: { error?: unknown } } | null)?.identity;
    return identity?.error === "step_up_required";
  }
  return wantsStepUp(outcome.value);
}

/**
 * Wrap a server action so every invocation is a keyed press. `press` (fixed, or decided from the
 * arguments) is for an operator's button; leave it out for loads and background calls.
 *
 * STEP-UP REPLAYS HERE, ONCE, FOR EVERY ACTION. A `step_up_required` answer (a result or a thrown
 * refusal) opens "Confirm it's you"; when the person confirms, the SAME call is sent exactly once
 * more as a new press, and its answer is what the caller gets — so Approve, a grant or a Retry
 * lands without a second press (BOSSAPP-510). ActionForm relies on this; it does not replay.
 */
export function callAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
  press?: Press | ((...args: A) => Press | undefined),
  /** The permission the action declares (lib/identity/action-permissions.ts). It names the
   *  permission in the step-up sheet; a `forbidden` refreshes the console's mirror so the
   *  control disables. The API decides. */
  permission?: Permission,
): (...args: A) => Promise<R> {
  const once = (...args: A) => runPress<R>({
    press: typeof press === "function" ? press(...args) : press,
    fingerprint: fingerprintOf(action, args),
  }, (key) => runPressedAction({ [INTENT_FIELD]: key } as { __boss_intent: string },
                               action, ...argsForPress(args, key)));
  return async (...args: A) => {
    let first: { value?: R; error?: unknown };
    try { first = { value: await once(...args) }; } catch (error) { first = { error }; }
    const mirror = currentMirror();
    if (mirror && askedForStepUp(first)) {
      const reason = permission ? `This needs “${permissionName(permission)}”.` : "This needs a fresh check.";
      if (await mirror.stepUp(reason)) {
        const again = await once(...args);
        noteDecisionResult(again);
        return again;
      }
    } else if (mirror && !("error" in first) && wasForbidden(first.value)) {
      mirror.refresh().catch(() => undefined);
    }
    if ("error" in first) throw first.error;
    // An accepted answer to a decision shows on every gate at once (lib/decided-gates.ts).
    noteDecisionResult(first.value);
    return first.value as R;
  };
}
