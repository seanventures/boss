"use client";

/**
 * WHO IS SIGNED IN, AND WHAT THEY HOLD — mirrored for the console's UX (IDENTITY.md §6.5).
 *
 * The provider holds the frozen contract's answers (/v1/identity/me and /me/permissions at the
 * org and the active project) and exposes them through can(): every area, page and action asks
 * it, and a control the viewer cannot use is DISABLED WITH THE REASON, never hidden (D7). The
 * server already read the same answers for the first paint (lib/identity/server.ts), so nothing
 * flickers from enabled to disabled and no page 403s after it loads.
 *
 * It refetches when the identity epoch moves: on focus, every 30 s while visible, when anything
 * dispatches `boss:identity-epoch` (the seam for the console socket's push, IDENTITY_BUILD §S4),
 * and after any refusal. The API re-checks every call; nothing here authorizes anything.
 *
 * Outside a provider (a unit test mounting one panel) useCan answers "allowed, not mirrored":
 * the console shell always mounts one (tests/identity-mirroring.test.ts), and the API stays the
 * authority either way.
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { snapshotFrom, ME_PATH, ME_PERMISSIONS_PATH, type CanResult, type MePermissionsResponse, type MeResponse, type PermissionSnapshot } from "../../lib/identity/contract";
import type { Permission } from "../../lib/identity/permissions.gen";
import { checkWith } from "../../lib/identity/check";
import { IdentityError, idGet, setCsrfToken } from "../../lib/identity/api";
import { StepUpSheet } from "./step-up-sheet";
import { currentMirror, installMirror, liveMirror, registerLiveMirror, type Mirror } from "../../lib/identity/mirror";
import { installReadGate } from "../../lib/identity/read-gates";
import { installGestureTracking, isInteractive } from "../../lib/user-gesture";

export const EPOCH_EVENT = "boss:identity-epoch";

type StepUpAsk = { reason: string; resolve: (ok: boolean) => void };

type Ctx = {
  me: MeResponse | null;
  org: PermissionSnapshot | null;
  project: PermissionSnapshot | null;
  projectId: string | null;
  stub: boolean;
  mirrored: boolean;
  check: (permission: Permission, projectId?: string | null) => CanResult;
  refresh: () => Promise<void>;
  /** Ask for a fresh second factor; resolves true when the person confirmed. */
  stepUp: (reason: string) => Promise<boolean>;
};

const IdentityCtx = createContext<Ctx | null>(null);

export type IdentityProviderProps = {
  me: MeResponse | null;
  org: MePermissionsResponse | null;
  project?: MePermissionsResponse | null;
  projectId?: string | null;
  stub?: boolean;
  /**
   * The identity API could not answer (503 identity_unavailable — before S1 lands, or an
   * outage). The console then does not mirror: every control stays usable, a banner says so,
   * and the API decides each call exactly as it always does.
   */
  unavailable?: boolean;
  children: ReactNode;
};

export { checkWith } from "../../lib/identity/check";

export function IdentityProvider({ me: initialMe, org: initialOrg, project: initialProject = null, projectId = null, stub = false, unavailable = false, children }: IdentityProviderProps) {
  const [me, setMe] = useState<MeResponse | null>(initialMe);
  const [org, setOrg] = useState<PermissionSnapshot | null>(initialOrg ? snapshotFrom(initialOrg) : null);
  const [project, setProject] = useState<PermissionSnapshot | null>(initialProject ? snapshotFrom(initialProject) : null);
  const [ask, setAsk] = useState<StepUpAsk | null>(null);
  const epochRef = useRef<number | null>(initialOrg?.epoch ?? null);

  // Set during render, not in an effect: a child's mount effect (a dry-run preview, a first
  // write) runs BEFORE this provider's effects, and would otherwise go out without the token.
  // Only when the token it holds is new: a session rotated since (step-up, a new factor) has a
  // newer token in lib/identity/api.ts, and an older render must not put the stale one back.
  const csrfSet = useRef<string | null>(null);
  if (me?.csrf_token && me.csrf_token !== csrfSet.current) {
    csrfSet.current = me.csrf_token;
    setCsrfToken(me.csrf_token);
  }

  const loadPermissions = useCallback(async () => {
    const [o, p] = await Promise.all([
      idGet<MePermissionsResponse>(ME_PERMISSIONS_PATH),
      projectId ? idGet<MePermissionsResponse>(`${ME_PERMISSIONS_PATH}?project_id=${encodeURIComponent(projectId)}`) : Promise.resolve(null),
    ]);
    setOrg(snapshotFrom(o));
    setProject(p ? snapshotFrom(p) : null);
    epochRef.current = o.epoch;
  }, [projectId]);

  const refresh = useCallback(async () => {
    try {
      const next = await idGet<MeResponse>(ME_PATH);
      setMe(next);
      if (next.epoch !== epochRef.current) await loadPermissions();
    } catch (error) {
      if (error instanceof IdentityError && error.status === 401 && error.code === "unauthenticated" && typeof window !== "undefined") {
        const here = window.location.pathname + window.location.search;
        window.location.assign(`/login?next=${encodeURIComponent(here)}`);
      }
    }
  }, [loadPermissions]);

  useEffect(() => {
    if (projectId && (!project || project.scope.kind !== "project" || project.scope.projectId !== projectId)) {
      loadPermissions().catch(() => undefined);
    }
  }, [projectId, project, loadPermissions]);

  useEffect(() => {
    const onEpoch = () => { refresh(); };
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener(EPOCH_EVENT, onEpoch);
    window.addEventListener("focus", onEpoch);
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 30_000);
    return () => {
      window.removeEventListener(EPOCH_EVENT, onEpoch);
      window.removeEventListener("focus", onEpoch);
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const mirrored = !(unavailable && !org);
  const check = useCallback((permission: Permission, pid?: string | null) =>
    mirrored ? checkWith(org, project, pid === undefined ? projectId : pid, permission) : UNMIRRORED.check(permission, pid),
  [org, project, projectId, mirrored]);

  // THE SHEET IS FOR A PRESS, NEVER FOR A PAGE VIEW (lib/user-gesture.ts). Every step-up path —
  // apiWrite, callAction, useGuardedCall, the client forms — asks through here, so this is the
  // one place that decides: a write the person did not start in the last few seconds (an
  // effect, a timer, a background sync) is answered "not confirmed" at once and its caller
  // shows its own inline state. "Confirm it's you" no longer appears on a plain refresh.
  //
  // ONE SHEET, EVERY ASKER ANSWERED, NO PROMISE LEFT WAITING (boss-1, 2026-11-06: Approve on a
  // Deploy step's stack deployment got one 401 step_up_required, no sheet, and every later press
  // went nowhere). A second ask while the sheet is open JOINS it — it used to replace the first
  // ask, whose caller then waited forever. An ask made of a provider that is not mounted (React
  // rendered it and threw it away, or it has left the page) goes to the provider that is
  // (lib/identity/mirror.ts liveMirror), or is answered "not confirmed" at once; and a provider
  // leaving the page answers its open ask "not confirmed". A press is never wedged behind a
  // sheet nobody can see.
  useEffect(() => { installGestureTracking(); }, []);
  const mounted = useRef(false);
  const waiting = useRef<{ reason: string; resolvers: ((ok: boolean) => void)[] } | null>(null);
  const settle = useCallback((ok: boolean) => {
    const open = waiting.current;
    waiting.current = null;
    setAsk(null);
    for (const resolve of open?.resolvers ?? []) resolve(ok);
  }, []);
  const stepUp = useCallback((reason: string): Promise<boolean> => {
    installGestureTracking();
    if (!mounted.current) {
      const live = liveMirror();
      return live && live !== mirrorRef.current ? live.stepUp(reason) : Promise.resolve(false);
    }
    if (!isInteractive()) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      if (waiting.current) { waiting.current.resolvers.push(resolve); return; }
      waiting.current = { reason, resolvers: [resolve] };
      setAsk({ reason, resolve: settle });
    });
  }, [settle]);

  // Code outside components (lib/call-action.ts, the read gate) mirrors through the same check
  // and step-up. Installed DURING RENDER as well as in the effect: a child's mount effect (the
  // shell's first reads) runs before this provider's effects, and the read gate must already
  // know who is looking (lib/identity/read-gates.ts).
  const mirror = useMemo<Mirror>(() => ({ check: (p: Permission) => check(p), stepUp, refresh, projectId }),
    [check, stepUp, refresh, projectId]);
  const mirrorRef = useRef<Mirror>(mirror);
  if (typeof window !== "undefined" && currentMirror() !== mirror) {
    installMirror(mirror);
    installReadGate();
  }
  useEffect(() => {
    mirrorRef.current = mirror;
    installMirror(mirror);
  }, [mirror]);
  // On the page: registered as live; leaving it, the mirror passes to the provider still here
  // (never nulled under it) and the open ask, if any, is answered "not confirmed".
  useEffect(() => {
    mounted.current = true;
    const leave = registerLiveMirror(mirrorRef);
    return () => {
      mounted.current = false;
      leave();
      const open = waiting.current;
      waiting.current = null;
      for (const resolve of open?.resolvers ?? []) resolve(false);
    };
  }, []);

  const value = useMemo<Ctx>(() => ({ me, org, project, projectId, stub, mirrored, check, refresh, stepUp }),
    [me, org, project, projectId, stub, mirrored, check, refresh, stepUp]);

  return (
    <IdentityCtx.Provider value={value}>
      {children}
      {ask ? (
        <StepUpSheet
          reason={ask.reason}
          me={me}
          onDone={(ok) => {
            ask.resolve(ok);
            setAsk(null);
            if (ok) loadPermissions().catch(() => undefined);
          }}
        />
      ) : null}
    </IdentityCtx.Provider>
  );
}

const UNMIRRORED: Ctx = {
  me: null, org: null, project: null, projectId: null, stub: false, mirrored: false,
  check: () => ({ allowed: true, reason: "", needsStepUp: false }),
  refresh: async () => undefined,
  stepUp: async () => false,
};

export function useIdentity(): Ctx {
  return useContext(IdentityCtx) ?? UNMIRRORED;
}

/** Can the viewer do this, here? `projectId` defaults to the console's active project. */
export function useCan(permission: Permission, projectId?: string | null): CanResult {
  return useIdentity().check(permission, projectId);
}

/** `<Can permission="workflow:publish">{(allowed, why) => <button disabled={!allowed} title={why}>…}</Can>` */
export function Can({ permission, projectId, children }: {
  permission: Permission; projectId?: string | null;
  children: (allowed: boolean, why: string, needsStepUp: boolean) => ReactNode;
}) {
  const r = useCan(permission, projectId);
  return <>{children(r.allowed, r.reason, r.needsStepUp)}</>;
}

/**
 * Run a write that may need a fresh second factor: on `401 step_up_required` the step-up sheet
 * opens, and the write is retried once when the person confirms. Any other refusal is thrown.
 */
export function useGuardedCall() {
  const { stepUp, refresh } = useIdentity();
  return useCallback(async <T,>(reason: string, call: () => Promise<T>): Promise<T> => {
    try {
      return await call();
    } catch (error) {
      if (error instanceof IdentityError && error.stepUp) {
        if (await stepUp(reason)) return call();
        throw new IdentityError(401, "step_up_cancelled");
      }
      if (error instanceof IdentityError && (error.status === 403 || error.status === 401)) refresh();
      throw error;
    }
  }, [stepUp, refresh]);
}

type CanButtonProps = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "title"> & {
  permission: Permission;
  projectId?: string | null;
  /** The tooltip when allowed (≤ 14 words). When refused, the reason replaces it. */
  tip?: string;
};

/**
 * A button that mirrors the viewer's access: refused, it stays visible and focusable, reads as
 * disabled (aria-disabled), says why in its tooltip and to a screen reader, and does nothing.
 */
export function CanButton({ permission, projectId, tip, onClick, children, disabled, className, ...rest }: CanButtonProps) {
  const r = useCan(permission, projectId);
  const off = !r.allowed || Boolean(disabled);
  return (
    <button
      type="button"
      {...rest}
      className={className}
      aria-disabled={off || undefined}
      data-permission={permission}
      data-can={r.allowed ? "yes" : "no"}
      title={!r.allowed ? r.reason : tip}
      onClick={(event) => {
        if (off) { event.preventDefault(); return; }
        onClick?.(event);
      }}
    >
      {children}
      {!r.allowed ? <span className="idn-sr">{r.reason}</span> : null}
    </button>
  );
}
