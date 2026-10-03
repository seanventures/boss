"use client";

/**
 * The few pieces of avatar motion that need JavaScript. Everything else is CSS keyed off the
 * SVG's `data-state` (agent-world.css, `agav-*`).
 *
 *   useAvatarMotion — pause a face's loops while it is off screen or the tab is hidden, so a
 *     roster of forty faces costs nothing when nobody is looking (spec §7). NB: an automation
 *     tab is hidden, so a test that expects motion must check document.visibilityState first.
 *   useSpeaking — "speaking" per streamed chunk, decaying to "thinking" 300ms after the last
 *     one, so the mouth moves with the words and stops when they do.
 *   useWaveOnFiveClicks — the easter egg: five clicks within two seconds, one wave.
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

import type { AvatarState } from "../../lib/agent-world-types";

export function useAvatarMotion(ref: RefObject<HTMLElement | null>, live: boolean): void {
  useEffect(() => {
    const el = ref.current;
    if (!el || !live) return;
    let onScreen = true;
    const apply = () => {
      const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
      if (hidden || !onScreen) el.setAttribute("data-paused", "true");
      else el.removeAttribute("data-paused");
    };
    let io: IntersectionObserver | null = null;
    if (typeof IntersectionObserver !== "undefined") {
      io = new IntersectionObserver((entries) => {
        onScreen = entries.some((e) => e.isIntersecting);
        apply();
      });
      io.observe(el);
    }
    document.addEventListener("visibilitychange", apply);
    apply();
    return () => {
      io?.disconnect();
      document.removeEventListener("visibilitychange", apply);
      el.removeAttribute("data-paused");
    };
  }, [ref, live]);
}

export const SPEAKING_DECAY_MS = 300;

/** Feed it `chunk()` per streamed token batch; read `state` for the face. */
export function useSpeaking(inFlight: boolean): { state: AvatarState; chunk(): void } {
  const [speaking, setSpeaking] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunk = useCallback(() => {
    setSpeaking(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setSpeaking(false), SPEAKING_DECAY_MS);
  }, []);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => {
    if (!inFlight) {
      if (timer.current) clearTimeout(timer.current);
      setSpeaking(false);
    }
  }, [inFlight]);
  return { state: speaking ? "speaking" : inFlight ? "thinking" : "idle", chunk };
}

export const WAVE_CLICKS = 5;
export const WAVE_WINDOW_MS = 2000;
export const WAVE_MS = 1200;

export function useWaveOnFiveClicks(): { waving: boolean; onClick(): void } {
  const clicks = useRef<number[]>([]);
  const [waving, setWaving] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const onClick = useCallback(() => {
    const now = Date.now();
    clicks.current = [...clicks.current.filter((t) => now - t < WAVE_WINDOW_MS), now];
    if (clicks.current.length >= WAVE_CLICKS) {
      clicks.current = [];
      setWaving(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setWaving(false), WAVE_MS);
    }
  }, []);
  return { waving, onClick };
}
