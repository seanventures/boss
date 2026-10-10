"use client";

import React, { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

/** Nonmodal help escapes the console's containment and returns to the actual opener. */
export function HelpPopover({ id, opener, onClose, children }: {
  id: string; opener: HTMLElement; onClose: () => void; children: React.ReactNode;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const close = () => {
    onClose();
    if (opener.isConnected) opener.focus();
  };
  useEffect(() => {
    closeButton.current?.focus();
    const outside = (event: PointerEvent) => {
      if (!surface.current?.contains(event.target as Node) && !opener.contains(event.target as Node)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
      if (opener.isConnected) opener.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [opener, onClose]);
  return createPortal(
    <div ref={surface} id={id} className="cc-help-pop" role="dialog" aria-label="What you can do here">
      <div className="cc-help-head">
        <span>What you can do here</span>
        <button ref={closeButton} type="button" className="cc-help-close" onClick={close}>Close help</button>
      </div>
      <div className="cc-help-content">{children}</div>
    </div>, document.body,
  );
}
