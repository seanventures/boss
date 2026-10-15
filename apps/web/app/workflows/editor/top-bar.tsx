"use client";

/**
 * The editor's one bar: where you are, what this is, and what you can do with it.
 *
 * Left: back to the Library, the name (edited in place), the version, Standard/Yours, the
 * "You have a copy" door and the unsaved dot. Right: undo/redo, Visual | JSON, Describe, Check,
 * Start, Save and ⋯. Every explanation is a tooltip; a control that is unavailable says why in
 * its tooltip, which is why those are aria-disabled rather than disabled (a disabled button
 * gets no hover).
 *
 * Save, Check, Start and the leave guard are the save flow's (`useSaveFlow`); this bar only
 * hosts its buttons, and the save flow anchors its popovers on them through `saveRef` and
 * `startRef`.
 */
import {
  ArrowClockwise, ArrowCounterClockwise, ArrowLeft, DotsThree, PencilSimple, Play, Sparkle,
} from "@phosphor-icons/react";
import React, { useRef, useState, type RefObject } from "react";

import { formatDate } from "../../../lib/datetime";
import type { EditorState } from "../../../lib/workflow-editor-types";
import { Popover } from "./inspector/popover";
import { CHECK_TIP } from "./save/use-save-flow";

export type VersionRow = { version: number; published_at: string; is_active: boolean };

export const STANDARD_TIP = "Shipped with BOSS. Saving makes your own copy.";
export const VERSION_TIP = "Each save is a new version. Running work keeps its version.";
export const NO_DELETE_TIP = "Standards can't be deleted.";

/** A button that stays focusable and hoverable when unavailable, so its tooltip can say why. */
function Btn({ blocked, tip, onClick, className, children, testId, label, pressed, buttonRef, role }: {
  blocked?: string | false | null; tip?: string; onClick(): void;
  className: string; children: React.ReactNode; testId?: string; label?: string; pressed?: boolean;
  buttonRef?: React.Ref<HTMLButtonElement>; role?: string;
}) {
  return (
    <button type="button" ref={buttonRef} className={className} aria-disabled={blocked ? true : undefined}
            data-tip={(blocked || tip) || undefined} aria-label={label} aria-pressed={pressed} role={role}
            data-testid={testId}
            onClick={() => { if (!blocked) onClick(); }}>
      {children}
    </button>
  );
}

export function TopBar(p: {
  state: EditorState;
  dirty: boolean;
  view: "visual" | "json";
  onView(v: "visual" | "json"): void;
  onBack(): void;
  onName(name: string): void;
  nameError?: string | null;
  autoFocusName?: boolean;
  canUndo: boolean; canRedo: boolean; onUndo(): void; onRedo(): void;
  loadVersions(): Promise<VersionRow[]>;
  onOpenVersion(v: number): void;
  onRestoreVersion(): void;
  onLeaveVersion(): void;
  onOpenCopy(): void;
  onDescribe(text: string): Promise<void>;
  describeBlocked?: string | null;
  onCheck(): void; checking: boolean; checkBlocked?: string | null;
  onStart(): void; startBlocked?: string | null; startRef: RefObject<HTMLButtonElement | null>;
  /** Run the saved version as a test with pinned step outputs (#512). */
  onTestRun?(): void; testRunBlocked?: string | null;
  onSave(): void; saveLabel: string; saveBlocked?: string | null; saveTip: string; saving: boolean;
  saveRef: RefObject<HTMLButtonElement | null>;
  onDuplicate(): void; duplicateBlocked?: string | null; onUseFor(): void; onChanges(): void; onDiscard(): void; onDelete(): void;
  deleteBlocked?: string | null;
  readOnly: boolean;
}) {
  const { state, dirty, view } = p;
  const meta = state.meta;
  const jsonLine = state.jsonError ? `Fix the JSON first (line ${state.jsonError.line})` : null;

  const [pop, setPop] = useState<null | "versions" | "menu" | "describe" | "discard" | "delete">(null);
  const versionsRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const describeRef = useRef<HTMLButtonElement>(null);
  const [versions, setVersions] = useState<VersionRow[] | null>(null);
  const [describeText, setDescribeText] = useState("");
  const [describing, setDescribing] = useState(false);
  const close = () => setPop(null);

  const openVersions = async () => {
    setPop("versions");
    setVersions(null);
    try { setVersions(await p.loadVersions()); } catch { setVersions([]); }
  };

  const viewing = meta.readOnlyVersion ?? null;

  return (
    <header className="wf-topbar" data-testid="wf-topbar">
      <div className="wf-topbar-row" data-testid="wf-topbar-row">
        <div className="wf-bar-identity" data-testid="wf-bar-identity">
          <button type="button" className="wf-btn wf-btn-ghost wf-back" data-testid="wf-back" onClick={p.onBack}>
            <ArrowLeft size={14} weight="bold" aria-hidden />Library
          </button>

          <label className="wf-name" data-error={p.nameError ? "true" : undefined}>
            <input className="wf-name-input" value={meta.name} aria-label="Name" readOnly={p.readOnly}
                   autoFocus={p.autoFocusName} data-testid="wf-name" aria-invalid={p.nameError ? true : undefined}
                   data-tip={meta.name.trim() || undefined} data-tip-when="cut"
                   onFocus={(e) => { if (p.autoFocusName) e.currentTarget.select(); }}
                   onChange={(e) => p.onName(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") e.currentTarget.blur(); }} />
            <PencilSimple size={14} weight="bold" className="wf-name-pencil" aria-hidden />
            {p.nameError ? <span className="wf-name-error" role="alert">{p.nameError}</span> : null}
          </label>

          {viewing !== null ? (
            <span className="wf-chip" data-tone="amber" data-testid="wf-viewing">
              Viewing v{viewing}
              <button type="button" className="wf-link" onClick={p.onRestoreVersion} data-testid="wf-restore"
                      data-tip="Make this the draft. Save publishes it as the next version.">Restore</button>
              <button type="button" className="wf-link" onClick={p.onLeaveVersion} aria-label="Back to the current version"
                      data-tip="Back to the current version" data-testid="wf-leave-version">×</button>
            </span>
          ) : meta.version !== null ? (
            <button type="button" ref={versionsRef} className="wf-chip wf-chip-btn" data-tip={VERSION_TIP}
                    aria-haspopup="dialog" onClick={() => void openVersions()} data-testid="wf-version">
              v{meta.version}
            </button>
          ) : null}

          {meta.is_catalogue ? (
            <span className="wf-chip" data-tone="accent" data-tip={STANDARD_TIP} data-testid="wf-kind">Standard</span>
          ) : meta.definition_id ? (
            <span className="wf-chip" data-testid="wf-kind">Yours</span>
          ) : null}

          {meta.is_catalogue && meta.copy ? (
            <button type="button" className="wf-chip wf-chip-btn wf-open-copy" data-tone="accent" onClick={p.onOpenCopy}
                    data-tip={meta.copy.name} data-testid="wf-open-copy">
              <span className="wf-cut">You have a copy · Open</span>
            </button>
          ) : null}

          {dirty ? <span className="wf-dirty" data-tip="Unsaved changes" role="img" aria-label="Unsaved changes"
                         data-testid="wf-dirty" /> : null}
        </div>

        <div className="wf-bar-actions" data-testid="wf-bar-actions">
          <div className="wf-bar-tools">
            <div className="wf-bar-group">
              <Btn className="wf-icon-btn" blocked={!p.canUndo && "Nothing to undo"} tip="Undo (Ctrl+Z)" label="Undo"
                   onClick={p.onUndo} testId="wf-undo">
                <ArrowCounterClockwise size={16} weight="bold" aria-hidden />
              </Btn>
              <Btn className="wf-icon-btn" blocked={!p.canRedo && "Nothing to redo"} tip="Redo (Ctrl+Shift+Z)" label="Redo"
                   onClick={p.onRedo} testId="wf-redo">
                <ArrowClockwise size={16} weight="bold" aria-hidden />
              </Btn>
            </div>

            <div className="wf-seg" role="group" aria-label="View">
              <Btn className="wf-seg-btn" pressed={view === "visual"} blocked={view === "json" && jsonLine}
                   onClick={() => p.onView("visual")} testId="wf-view-visual">Visual</Btn>
              <Btn className="wf-seg-btn" pressed={view === "json"} onClick={() => p.onView("json")} testId="wf-view-json">
                JSON
              </Btn>
            </div>

            <Btn className="wf-icon-btn" buttonRef={describeRef} label="Describe" testId="wf-describe"
                 blocked={p.readOnly ? `Viewing v${viewing}` : p.describeBlocked}
                 tip="Describe it. BOSS drafts the steps." onClick={() => setPop("describe")}>
              <Sparkle size={16} weight="bold" aria-hidden />
            </Btn>
          </div>

          <div className="wf-bar-commit" data-testid="wf-bar-commit">
            <Btn className="wf-btn wf-btn-secondary" tip={CHECK_TIP} blocked={(p.checking && "Checking…") || jsonLine || p.checkBlocked}
                 onClick={p.onCheck} testId="wf-check">Check</Btn>
            <Btn className="wf-btn wf-btn-secondary" buttonRef={p.startRef} blocked={p.startBlocked}
                 tip="Start a run of this workflow" testId="wf-start" onClick={p.onStart}>
              Start<Play size={12} weight="fill" aria-hidden />
            </Btn>
            {p.onTestRun ? (
              <Btn className="wf-btn wf-btn-secondary" blocked={p.testRunBlocked}
                   tip="Run it as a test: pin step outputs, nothing spends unless you allow it"
                   testId="wf-test-run-open" onClick={p.onTestRun}>
                Test run
              </Btn>
            ) : null}
            <Btn className="wf-btn wf-btn-primary" buttonRef={p.saveRef} blocked={p.saveBlocked} tip={p.saveTip}
                 onClick={p.onSave} testId="wf-save">{p.saving ? "Saving…" : p.saveLabel}</Btn>
            <Btn className="wf-icon-btn" buttonRef={menuRef} label="More" tip="More" onClick={() => setPop("menu")}
                 testId="wf-more">
              <DotsThree size={18} weight="bold" aria-hidden />
            </Btn>
          </div>
        </div>
      </div>

      <Popover anchor={versionsRef} open={pop === "versions"} onClose={close} label="Versions" width={220}
               testId="wf-versions">
        <ul className="wf-menu">
          {versions === null ? <li className="wf-menu-note" aria-busy="true">…</li> : null}
          {(versions ?? []).map((v) => {
            const current = v.version === meta.version;
            const blocked = !current && dirty ? "Save or discard changes first" : null;
            return (
              <li key={v.version}>
                <Btn className="wf-menu-item" blocked={current ? "Open now" : blocked}
                     onClick={() => { close(); p.onOpenVersion(v.version); }} testId={`wf-open-v${v.version}`}>
                  <span>v{v.version}</span>
                  <span className="wf-menu-meta">{current ? "Current" : `Open · ${formatDate(v.published_at, "")}`}</span>
                </Btn>
              </li>
            );
          })}
        </ul>
      </Popover>

      <Popover anchor={describeRef} open={pop === "describe"} onClose={close} label="Describe" width={320}
               testId="wf-describe-pop">
        <div className="wf-pop-form">
          <textarea className="wf-input" rows={3} placeholder="What should it do?" aria-label="What should it do?"
                    value={describeText} onChange={(e) => setDescribeText(e.target.value)} autoFocus />
          <div className="wf-pop-actions">
            <button type="button" className="wf-btn wf-btn-primary" disabled={!describeText.trim() || describing}
                    data-testid="wf-describe-go"
                    onClick={async () => {
                      setDescribing(true);
                      try { await p.onDescribe(describeText.trim()); close(); setDescribeText(""); }
                      catch { /* the editor has said why */ }
                      finally { setDescribing(false); }
                    }}>
              {describing ? "Drafting…" : "Draft steps"}
            </button>
          </div>
        </div>
      </Popover>

      <Popover anchor={menuRef} open={pop === "menu" || pop === "discard" || pop === "delete"}
               onClose={close} label="More" role={pop === "menu" ? "menu" : "dialog"} width={220}
               testId="wf-menu">
        {pop === "discard" ? (
          <div className="wf-pop-form">
            <div className="wf-pop-title">Discard changes?</div>
            <div className="wf-pop-actions">
              <button type="button" className="wf-btn wf-btn-danger" autoFocus onClick={() => { close(); p.onDiscard(); }}
                      data-testid="wf-discard-confirm">Discard</button>
            </div>
          </div>
        ) : pop === "delete" ? (
          <div className="wf-pop-form">
            <div className="wf-pop-title">Delete “{meta.name}”?</div>
            <div className="wf-pop-actions">
              <button type="button" className="wf-btn wf-btn-danger" autoFocus onClick={() => { close(); p.onDelete(); }}
                      data-testid="wf-delete-confirm">Delete</button>
            </div>
          </div>
        ) : (
          <ul className="wf-menu" role="none">
            <li role="none"><Btn role="menuitem" className="wf-menu-item" onClick={() => { close(); p.onDuplicate(); }}
                                 blocked={(p.readOnly && `Viewing v${viewing}`) || p.duplicateBlocked} testId="wf-menu-duplicate">Duplicate</Btn></li>
            <li role="none"><Btn role="menuitem" className="wf-menu-item" onClick={() => { close(); p.onUseFor(); }}
                                 testId="wf-menu-use-for">Use for…</Btn></li>
            <li role="none"><Btn role="menuitem" className="wf-menu-item" blocked={!state.loaded && "Nothing published yet"}
                                 onClick={() => { close(); p.onChanges(); }} testId="wf-menu-changes">Changes</Btn></li>
            <li role="none"><Btn role="menuitem" className="wf-menu-item" blocked={!dirty && "No changes"}
                                 onClick={() => setPop("discard")} testId="wf-menu-discard">Discard changes</Btn></li>
            <li role="none"><Btn role="menuitem" className="wf-menu-item wf-menu-danger" blocked={p.deleteBlocked}
                                 onClick={() => setPop("delete")} testId="wf-menu-delete">Delete</Btn></li>
          </ul>
        )}
      </Popover>
    </header>
  );
}
