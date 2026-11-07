"use client";

/**
 * THE ONE "ARE YOU SURE?" on project pages (13-12, 08-05): the console's own sheet (aria-modal,
 * Esc closes, focus kept inside), never the browser's window.confirm(). It says what happens and
 * where, then one primary action and Cancel. Enforced by tests/px-no-native-confirm.test.ts.
 */
import React from "react";
import { Warning } from "@phosphor-icons/react";

import { Sheet } from "../../components/identity/kit";

export function ConfirmSheet({ title, confirm, danger, busy, onConfirm, onCancel, children, testid = "px-confirm-sheet" }: {
  title: string; confirm: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onCancel: () => void; children?: React.ReactNode; testid?: string;
}) {
  return (
    <Sheet title={title} icon={danger ? <Warning size={18} className="idn-warn" aria-hidden /> : undefined} onClose={onCancel} testId={testid}>
      <div className="idn-lede2" data-copy="prose">{children}</div>
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn sm ghost" onClick={onCancel} data-testid={`${testid}-cancel`}>Cancel</button>
        <button type="button" className={`idn-btn sm${danger ? " danger solid" : " primary"}`} onClick={onConfirm} disabled={busy} data-autofocus data-testid={`${testid}-confirm`}>
          {busy ? "Working…" : confirm}</button>
      </div>
    </Sheet>
  );
}
