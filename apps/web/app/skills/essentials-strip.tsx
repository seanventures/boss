"use client";

/**
 * The first run, inside the Kit (spec §5.2 Essentials). Four tiles joined like the Studio lane , 
 * Open pull requests → Web research → File tickets → Email → Test crew, each with its state and
 * one button. It shows until all four are Ready (then one line), or until dismissed; `&setup=1`
 * brings it back. After a passing Test crew it is gone for good.
 *
 * Dismissal is a per-viewer convenience kept in localStorage, wrapped so a private window or
 * blocked storage only means the strip shows.
 */
import React from "react";
import { X } from "@phosphor-icons/react";

import type { Ability, AbilityStatus } from "../../lib/kit-types";
import { AbilityIcon } from "./ability-icons";
import { ACTION_OF, type CardAction, REPO_HREF } from "./ability-card";
import { PILL, PILL_TONE, TIP } from "./kit-copy";

const TILE_BUTTON: Partial<Record<AbilityStatus, string>> = {
  ready: "Done ✓", untested: "Test", unheld: "Give to agents", setup: "Set up", needs_repo: "Connect repo ↗",
  failing: "Fix", review: "Review", off: "Turn on", testing: "Testing…",
};

export const dismissKey = (projectId: string) => `boss.kit.essentials.dismissed.${projectId}`;
export const crewKey = (projectId: string) => `boss.kit.crew.passed.${projectId}`;

export function readFlag(key: string): boolean {
  try {
    return typeof window !== "undefined" && window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

export function writeFlag(key: string, on: boolean): void {
  try {
    if (on) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch { /* storage blocked: the strip just shows */ }
}

export function EssentialsStrip({ essentials, testing, onOpen, onAction, onTestCrew, onDismiss }: {
  essentials: Ability[];
  testing: Set<string>;
  onOpen: (key: string) => void;
  onAction: (key: string, action: CardAction) => void;
  onTestCrew: () => void;
  onDismiss: () => void;
}) {
  const allReady = essentials.length > 0 && essentials.every((a) => a.status === "ready");
  if (allReady) {
    return (
      <div className="kit-ess kit-ess-done" data-testid="kit-essentials-done">
        <span className="kit-ess-title" data-tip={TIP.essentials}>Essentials ready</span>
        <button type="button" className="kit-btn kit-btn-primary" data-tip={TIP.testCrew}
                data-testid="kit-test-crew" onClick={onTestCrew}>Test crew</button>
        <button type="button" className="kit-icon-btn" aria-label="Dismiss" data-testid="kit-essentials-dismiss"
                onClick={onDismiss}><X size={14} /></button>
      </div>
    );
  }
  const press = (a: Ability, status: AbilityStatus) => {
    const action = ACTION_OF[status];
    if (status === "ready") onOpen(a.key);
    else if (status === "setup") onAction(a.key, "connect");
    else if (action) onAction(a.key, action);
  };
  const statusOf = (a: Ability): AbilityStatus => (testing.has(a.key) ? "testing" : a.status);
  const done = essentials.filter((a) => a.status === "ready").length;
  const next = essentials.find((a) => a.status !== "ready");
  const nextStatus = next ? statusOf(next) : null;
  return (
    <section className="kit-ess" aria-label="Essentials" data-testid="kit-essentials">
      <header className="kit-ess-head">
        <span className="kit-ess-title" data-tip={TIP.essentials}>Essentials</span>
        <button type="button" className="kit-icon-btn" aria-label="Dismiss" data-testid="kit-essentials-dismiss"
                onClick={onDismiss}><X size={14} /></button>
      </header>
      {/* ON A PHONE, ONE LINE (K3): the four as dots (the next one ringed), the count, and the
          next one's button, four 240px tiles stacked pushed the first card a screen down. */}
      <div className="kit-ess-compact" data-testid="kit-essentials-compact" data-tip={TIP.essentials}>
        <span className="kit-ess-dots">
          {essentials.map((a, i) => {
            const status = statusOf(a);
            return (
              <button key={a.key} type="button" className="kit-ess-dot" data-status={status}
                      data-next={a === next || undefined}
                      aria-label={`${i + 1} ${a.label}, ${PILL[status]}`} data-tip={`${a.label}, ${PILL[status]}`}
                      data-testid={`kit-ess-dot-${a.key}`} onClick={() => onOpen(a.key)}>
                <span className="kit-dot" data-tone={PILL_TONE[status]} />
              </button>
            );
          })}
        </span>
        <span className="kit-faint">{done}/{essentials.length}</span>
        <span className="kit-spacer" />
        {next && nextStatus ? (
          ACTION_OF[nextStatus] === "repo" ? (
            <a className="kit-btn kit-btn-sm" href={REPO_HREF} data-tip={next.label} aria-label={`${next.label}, ${TILE_BUTTON[nextStatus]}`}
               data-testid="kit-ess-compact-action">{TILE_BUTTON[nextStatus]}</a>
          ) : (
            <button type="button" className="kit-btn kit-btn-sm" disabled={nextStatus === "testing"} data-tip={next.label}
                    aria-label={`${next.label}, ${TILE_BUTTON[nextStatus]}`}
                    data-testid="kit-ess-compact-action" onClick={() => press(next, nextStatus)}>
              {TILE_BUTTON[nextStatus]}
            </button>
          )
        ) : null}
        <button type="button" className="kit-icon-btn" aria-label="Dismiss" onClick={onDismiss}><X size={14} /></button>
      </div>
      <ol className="kit-ess-lane">
        {essentials.map((a, i) => {
          const status = statusOf(a);
          const action = ACTION_OF[status];
          return (
            <li key={a.key} className="kit-ess-tile" data-status={status} data-testid={`kit-ess-${a.key}`}
                onClick={() => onOpen(a.key)}>
              <span className="kit-ess-num">{i + 1}</span>
              <AbilityIcon name={a.icon} size={20} />
              <span className="kit-ess-label" data-tip={a.tip}>{a.label}</span>
              <span className="kit-dot" data-tone={PILL_TONE[status]} data-tip={`${PILL[status]}, ${a.status_tip}`} />
              {action === "repo" ? (
                <a className="kit-btn kit-btn-sm" href={REPO_HREF} onClick={(e) => e.stopPropagation()}
                   data-testid={`kit-ess-action-${a.key}`}>{TILE_BUTTON[status]}</a>
              ) : (
                <button type="button" className={`kit-btn kit-btn-sm${status === "ready" ? " kit-btn-done" : ""}`}
                        disabled={status === "testing"} data-testid={`kit-ess-action-${a.key}`}
                        onClick={(e) => { e.stopPropagation(); press(a, status); }}>
                  {TILE_BUTTON[status]}
                </button>
              )}
            </li>
          );
        })}
        <li className="kit-ess-tile kit-ess-final">
          <button type="button" className="kit-btn" data-tip={TIP.testCrew} data-testid="kit-test-crew"
                  onClick={onTestCrew}>Test crew</button>
        </li>
      </ol>
    </section>
  );
}
