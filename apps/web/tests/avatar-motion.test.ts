// @vitest-environment happy-dom
/**
 * Motion carries state, and only state (spec §2.5, §7, §12.4 test 12).
 *
 * Every loop on a face exists for a real status that a mapper produced, every state also has a
 * glyph and a word, every keyframe has a still under reduced motion, loops hold still off
 * screen and in a hidden tab, and nothing in the kit's CSS sets text under 0.75rem.
 */
import React, { act, useRef } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { avatarSvg, seededSpec } from "../lib/agent-avatar";
import {
  AVATAR_STATES, LIVE_STATES, STATE_WORDS, stateFromAct, stateFromPresence, stateFromRunPhase, stateFromStatus,
  stateFromTurn, withLifecycle,
} from "../lib/avatar-kit/states";
import { SPEAKING_DECAY_MS, useAvatarMotion, useSpeaking, useWaveOnFiveClicks } from "../app/agents/use-avatar-motion";
import { cssOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ALL = cssOnly("app/agents/agent-world.css");
const KIT = ALL.slice(ALL.indexOf(".ag-avatar"), ALL.indexOf("@media (prefers-reduced-motion: reduce)") + 200);
const DESIGN = cssOnly("app/design-system.css");

const keyframes = (css: string) => [...css.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
const used = (css: string) => [...css.matchAll(/animation(?:-name)?\s*:\s*([\w-]+)/g)].map((m) => m[1]).filter((n) => n !== "none");

describe("every animation is defined, used, and named agav-", () => {
  it("every agav- keyframe is referenced, and every reference is defined", () => {
    const defined = keyframes(ALL);
    const refs = used(ALL);
    expect(defined.length).toBeGreaterThanOrEqual(10);
    for (const k of defined) {
      expect(k.startsWith("agav-"), k).toBe(true);
      expect(refs, `${k} is never used`).toContain(k);
    }
    for (const r of refs) expect(defined.includes(r) || keyframes(DESIGN).includes(r), `${r} is not defined`).toBe(true);
    expect(new Set(defined).size).toBe(defined.length);
  });

  it("animates only transform and opacity", () => {
    for (const m of ALL.matchAll(/@keyframes\s+([\w-]+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)) {
      const props = [...m[2].matchAll(/([\w-]+)\s*:/g)].map((x) => x[1]);
      for (const p of props) expect(["transform", "opacity"], `${m[1]} animates ${p}`).toContain(p);
    }
  });

  it("turns every face's motion off under prefers-reduced-motion", () => {
    const reduced = /@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/.exec(ALL)?.[1] ?? "";
    expect(reduced).toMatch(/\.ag-av \*[^{]*\{\s*animation:\s*none !important/);
    // Every rule that animates a FACE (its parts, or the face itself) goes through .ag-av /
    // .ag-avatar, so the kit's still above covers it. Chrome around a face — the presence pill
    // fading in, a speech bubble, the birth certificate's stamp — is not a face, and has its own
    // still in the Agents section.
    for (const m of ALL.matchAll(/([^{}]+)\{[^{}]*animation:\s*agav-/g)) {
      if (!/\.av-|\.ag-face\b/.test(m[1])) continue;
      expect(m[1], m[1]).toMatch(/\.ag-av\b|\.ag-avatar\b/);
    }
  });

  it("pauses every loop when the face is marked paused", () => {
    expect(ALL).toMatch(/\.ag-avatar\[data-paused\] \.ag-av \*[^{]*\{\s*animation-play-state:\s*paused !important/);
  });

  it("never flashes: the needs-you halo cycles at 1Hz or slower", () => {
    const rule = /\[data-state="needs_you"\] \.av-halo \{ animation: agav-halo ([\d.]+)s[^;]*alternate/.exec(ALL);
    expect(rule).not.toBeNull();
    expect(Number(rule![1])).toBeGreaterThanOrEqual(0.5); // alternate: a full cycle is twice this
  });

  it("idle faces in small lists are still; only the hero sizes and the roster's big faces breathe", () => {
    // The roster card's face is 64px (the hero of its card), big enough for a calm breathe and
    // blink to read as life rather than noise. Chips, lanes and pickers stay still.
    expect(ALL).toMatch(/\[data-state="idle"\]:is\(\[data-lod="96"\], \[data-lod="320"\]\) \.av-body \{ animation: agav-breathe/);
    for (const m of ALL.matchAll(/([^{}]+)\{[^{}]*animation:\s*agav-(breathe|blink)/g)) {
      expect(m[1].includes('data-lod="96"') || m[1].includes(".ag-card-face"), m[1]).toBe(true);
    }
  });

  it("sets no text under 0.75rem", () => {
    const sizes = [...KIT.matchAll(/font(?:-size)?\s*:\s*(?:\d+\s+)?([\d.]+)(rem|px)/g)].map((m) => (m[2] === "px" ? Number(m[1]) / 16 : Number(m[1])));
    expect(sizes.length).toBeGreaterThan(0);
    for (const s of sizes) expect(s).toBeGreaterThanOrEqual(0.75);
  });

  it("uses only base tokens for colour in the kit's CSS", () => {
    expect(KIT).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(KIT).not.toMatch(/var\(--v2-/);
  });
});

describe("no state is shown by colour or motion alone", () => {
  it("every non-idle state has a word, a ring and a glyph badge from 24px", () => {
    for (const state of AVATAR_STATES) {
      expect(STATE_WORDS[state]).toBeTruthy();
      const s = avatarSvg(seededSpec("x"), { size: 24, state });
      if (state === "idle") { expect(s).not.toContain("av-badge"); continue; }
      expect(s, state).toContain('class="av-badge"');
      expect(s, state).toMatch(/class="av-ring/);
      expect(s, state).toMatch(/av-badge-glyph"><(rect|circle|path|g)/);
    }
  });

  it("draws the glyphs differently per state", () => {
    const glyph = (state: (typeof AVATAR_STATES)[number]) => /av-badge-glyph">(.*?)<\/g><\/g>/.exec(avatarSvg(seededSpec("x"), { size: 48, state }))?.[1];
    const glyphs = AVATAR_STATES.filter((s) => s !== "idle").map(glyph);
    expect(new Set(glyphs).size).toBe(glyphs.length);
  });

  it("tilts a stuck head and greys out paused and retired faces", () => {
    expect(avatarSvg(seededSpec("x"), { size: 48, state: "stuck" })).toContain('class="av-head" transform="rotate(4 50 50)"');
    for (const s of ["paused", "retired"] as const) expect(avatarSvg(seededSpec("x"), { size: 24, state: s })).toContain('class="av-char" opacity="0.55"');
    expect(ALL).toMatch(/\[data-state="paused"\][^{]*\[data-lod="48"\][^{]*\.av-char \{\s*filter: grayscale/);
  });
});

describe("states map from real status, one mapper per source", () => {
  it("acts", () => {
    expect(stateFromAct("awaiting_input")).toBe("needs_you");
    expect(stateFromAct("active", { kind: "step" })).toBe("working");
    expect(stateFromAct("active", { kind: "chat" })).toBe("thinking");
    expect(stateFromAct("active", { kind: "chat", tokens: true })).toBe("working");
    expect(stateFromAct("active", { kind: "seat", streaming: true })).toBe("speaking");
    expect(stateFromAct("complete")).toBe("done");
    expect(stateFromAct("error")).toBe("stuck");
    expect(stateFromAct("stale")).toBe("stuck");
    expect(stateFromAct("pending")).toBe("idle");
    expect(stateFromAct("skipped")).toBe("idle");
  });

  it("presence, including the API's in_panel", () => {
    expect(stateFromPresence({ state: "needs_you" })).toBe("needs_you");
    expect(stateFromPresence({ state: "in_panel" })).toBe("working");
    expect(stateFromPresence({ state: "nonsense" })).toBe("idle");
    expect(stateFromPresence(null)).toBe("idle");
  });

  it("run phases on the lane", () => {
    expect(["moving", "needs_you", "done", "failed", "future", "skipped"].map(stateFromRunPhase))
      .toEqual(["working", "needs_you", "done", "stuck", "idle", "idle"]);
  });

  it("raw step and task statuses", () => {
    expect(stateFromStatus("running")).toBe("working");
    expect(stateFromStatus("awaiting_approval")).toBe("needs_you");
    expect(stateFromStatus("completed")).toBe("done");
    expect(stateFromStatus("failed")).toBe("stuck");
    expect(stateFromStatus("queued")).toBe("idle");
    expect(stateFromStatus(undefined)).toBe("idle");
  });

  it("a streamed turn, and lifecycle over activity", () => {
    expect(stateFromTurn({ inFlight: true, streaming: false })).toBe("thinking");
    expect(stateFromTurn({ inFlight: true, streaming: true })).toBe("speaking");
    expect(stateFromTurn({ inFlight: false, streaming: false })).toBe("idle");
    expect(stateFromTurn({ inFlight: false, streaming: false, error: true })).toBe("stuck");
    expect(withLifecycle("working", "paused")).toBe("paused");
    expect(withLifecycle("working", "retired")).toBe("retired");
    expect(withLifecycle("working", "active")).toBe("working");
  });

  it("only live states loop", () => {
    expect([...LIVE_STATES].sort()).toEqual(["needs_you", "speaking", "thinking", "working"]);
  });
});

describe("the hooks", () => {
  let host: HTMLElement;
  let visibility: DocumentVisibilityState = "visible";
  let ioCallback: ((e: { isIntersecting: boolean }[]) => void) | null = null;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
    (globalThis as Record<string, unknown>).IntersectionObserver = class {
      constructor(cb: (e: { isIntersecting: boolean }[]) => void) { ioCallback = cb; }
      observe() {}
      disconnect() {}
    };
  });
  afterEach(() => { host.remove(); visibility = "visible"; vi.useRealTimers(); });

  function Face({ live }: { live: boolean }) {
    const ref = useRef<HTMLSpanElement>(null);
    useAvatarMotion(ref, live);
    return React.createElement("span", { ref, "data-testid": "face" });
  }

  it("holds a live face still off screen and in a hidden tab, and lets it move again", async () => {
    const root = createRoot(host);
    await act(async () => root.render(React.createElement(Face, { live: true })));
    const face = host.querySelector("[data-testid=face]")!;
    expect(face.hasAttribute("data-paused")).toBe(false);
    await act(async () => ioCallback!([{ isIntersecting: false }]));
    expect(face.getAttribute("data-paused")).toBe("true");
    await act(async () => ioCallback!([{ isIntersecting: true }]));
    expect(face.hasAttribute("data-paused")).toBe(false);
    visibility = "hidden";
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(face.getAttribute("data-paused")).toBe("true");
    visibility = "visible";
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(face.hasAttribute("data-paused")).toBe(false);
    await act(async () => root.unmount());
  });

  it("speaks per chunk and decays to thinking 300ms after the last one", async () => {
    vi.useFakeTimers();
    let api: ReturnType<typeof useSpeaking> | null = null;
    function Probe({ inFlight }: { inFlight: boolean }) { api = useSpeaking(inFlight); return null; }
    const root = createRoot(host);
    await act(async () => root.render(React.createElement(Probe, { inFlight: true })));
    expect(api!.state).toBe("thinking");
    await act(async () => api!.chunk());
    expect(api!.state).toBe("speaking");
    await act(async () => { vi.advanceTimersByTime(SPEAKING_DECAY_MS - 50); api!.chunk(); });
    await act(async () => { vi.advanceTimersByTime(SPEAKING_DECAY_MS - 50); });
    expect(api!.state).toBe("speaking");
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(api!.state).toBe("thinking");
    await act(async () => root.render(React.createElement(Probe, { inFlight: false })));
    expect(api!.state).toBe("idle");
    await act(async () => root.unmount());
  });

  it("waves on the fifth click inside two seconds, and only then", async () => {
    vi.useFakeTimers();
    let api: ReturnType<typeof useWaveOnFiveClicks> | null = null;
    function Probe() { api = useWaveOnFiveClicks(); return null; }
    const root = createRoot(host);
    await act(async () => root.render(React.createElement(Probe)));
    for (let i = 0; i < 4; i++) await act(async () => { api!.onClick(); vi.advanceTimersByTime(600); });
    expect(api!.waving).toBe(false); // clicks spread over 2.4s
    for (let i = 0; i < 5; i++) await act(async () => { api!.onClick(); vi.advanceTimersByTime(100); });
    expect(api!.waving).toBe(true);
    await act(async () => { vi.advanceTimersByTime(1200); });
    expect(api!.waving).toBe(false);
    await act(async () => root.unmount());
  });
});
