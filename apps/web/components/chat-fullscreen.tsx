"use client";

/**
 * CHAT FULL SCREEN, ONE CONTROL FOR EVERY CHAT WINDOW (operator, 2026-11-19).
 *
 * The captain drawer, the Ask BOSS dock, a role's talk sheet, a room and the workforce chat
 * are each a narrow panel; a long answer or a long brief wants the whole screen. Each window
 * puts `ChatFullscreenButton` in its header and `data-chat-full` on its own root (the window,
 * not the transcript inside it, so the header with who is answering comes along). The CSS in
 * globals.css (`[data-chat-full="1"]`) lays the window over the whole viewport.
 *
 * Esc leaves full screen FIRST, and only that: the drawer underneath also closes on Esc, and a
 * person who pressed Esc to get back to the panel must not lose the panel. Nothing is
 * remembered across reloads.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowsIn, ArrowsOut } from "@phosphor-icons/react";

export function useChatFullscreen(initial = false): { full: boolean; toggle: () => void; exit: () => void;
                                       attr: { "data-chat-full"?: "1" } } {
  const [full, setFull] = useState(initial);
  const toggle = useCallback(() => setFull((v) => !v), []);
  const exit = useCallback(() => setFull(false), []);
  useEffect(() => {
    if (!full) return;
    // CAPTURE ON WINDOW runs before the drawer's document listener, so stopping it here is
    // what keeps one Esc from also closing the window behind.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      setFull(false);
    };
    window.addEventListener("keydown", onKey, true);
    // The page behind must not scroll under a full-screen chat.
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = before;
    };
  }, [full]);
  return { full, toggle, exit, attr: full ? { "data-chat-full": "1" } : {} };
}

export function ChatFullscreenButton({ full, onToggle, className = "chat-full-btn", testid = "chat-fullscreen" }: {
  full: boolean; onToggle: () => void; className?: string; testid?: string;
}) {
  const label = full ? "Exit full screen" : "Full screen";
  return (
    <button type="button" className={className} onClick={onToggle}
            aria-pressed={full} aria-label={label} title={full ? "Exit full screen (Esc)" : "Full screen"}
            data-testid={testid}>
      {full ? <ArrowsIn size={15} aria-hidden /> : <ArrowsOut size={15} aria-hidden />}
    </button>
  );
}
