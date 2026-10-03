"use client";

/**
 * A 24px strip under a lane that is too long to fit: one dot per step and a box for the part
 * on screen. Dragging the box (or pressing anywhere on the strip) scrolls the lane there.
 * It only appears when the lane scrolls; a flow that fits never shows it.
 */
import React, { useCallback, useEffect, useRef } from "react";

export function Minimap({ viewport, controls, width, scale, dots, selected }: {
  /** The lane's scrolling element. */
  viewport: React.RefObject<HTMLElement | null>;
  /** The id of that element, for aria-controls. */
  controls?: string;
  /** The lane's natural width, in canvas units. */
  width: number;
  scale: number;
  dots: { id: string; x: number; phase?: string; problem?: boolean }[];
  selected?: string | null;
}) {
  const strip = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);

  const paint = useCallback(() => {
    const v = viewport.current;
    const b = box.current;
    if (!v || !b) return;
    const total = Math.max(1, width * scale);
    b.style.left = `${(v.scrollLeft / total) * 100}%`;
    b.style.width = `${Math.min(100, (v.clientWidth / total) * 100)}%`;
    const room = total - v.clientWidth;
    strip.current?.setAttribute("aria-valuenow", String(room > 0 ? Math.round((v.scrollLeft / room) * 100) : 0));
  }, [viewport, width, scale]);

  useEffect(() => {
    const v = viewport.current;
    if (!v) return;
    let frame: number | null = null;
    const onScroll = () => {
      if (frame === null) frame = window.requestAnimationFrame(() => { frame = null; paint(); });
    };
    paint();
    v.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      v.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [paint, viewport]);

  const scrollTo = useCallback((clientX: number, grab: number) => {
    const v = viewport.current;
    const s = strip.current;
    if (!v || !s) return;
    const r = s.getBoundingClientRect();
    if (!r.width) return;
    const fraction = (clientX - r.left - grab) / r.width;
    v.scrollLeft = fraction * width * scale;
  }, [viewport, width, scale]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const b = box.current?.getBoundingClientRect();
    const onBox = b && e.clientX >= b.left && e.clientX <= b.right;
    // Pressing the box keeps where it was grabbed; pressing the strip centres the box there.
    const grab = onBox && b ? e.clientX - b.left : (b?.width ?? 0) / 2;
    scrollTo(e.clientX, grab);
    const move = (ev: PointerEvent) => scrollTo(ev.clientX, grab);
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    e.preventDefault();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const v = viewport.current;
    if (!v) return;
    if (e.key === "ArrowRight") { v.scrollLeft += v.clientWidth / 2; e.preventDefault(); }
    if (e.key === "ArrowLeft") { v.scrollLeft -= v.clientWidth / 2; e.preventDefault(); }
  };

  return (
    <div className="wf-minimap" ref={strip} onPointerDown={onPointerDown} onKeyDown={onKeyDown}
         role="scrollbar" aria-label="Flow overview" aria-orientation="horizontal"
         aria-controls={controls} aria-valuemin={0} aria-valuemax={100} aria-valuenow={0}
         tabIndex={0} data-testid="wf-minimap">
      {dots.map((d) => (
        <span key={d.id} className="wf-minimap-dot" style={{ left: `${(d.x / width) * 100}%` }}
              data-phase={d.phase} data-problem={d.problem ? "true" : undefined}
              data-selected={d.id === selected ? "true" : undefined} />
      ))}
      <div className="wf-minimap-box" ref={box} />
    </div>
  );
}
