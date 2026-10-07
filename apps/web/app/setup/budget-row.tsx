"use client";

/** Budget: what metered models have spent against the monthly cap — the same cap Govern edits. */
import { GatedButton } from "../../components/gated";
import React, { forwardRef, useEffect, useState } from "react";

import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { SetupView } from "../../lib/setup-types";
import { toast, useBoard } from "./parts";
import { Pill, Tip } from "./pill";
import { SetupRow } from "./setup-row";
import { consoleHref } from "../url-intent";

const usd = (n: number) => (Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`);

export const BudgetRow = forwardRef<HTMLElement, { mode: string; focused: boolean }>(
  function BudgetRow({ mode, focused }, ref) {
    const { projectId, view, apply } = useBoard();
    const budget = view.budget;
    const [editing, setEditing] = useState(false);
    useEffect(() => { if (focused && budget.state === "spent") setEditing(true); },
              [focused, budget.state]);
    const [value, setValue] = useState(String(budget.monthly_cap_usd ?? budget.cap_usd));
    const [busy, setBusy] = useState(false);
    const [open, setOpen] = useState(false);
    // Govern ↗ lands on the Monthly cap row and keeps who you are (org=): built from the URL,
    // so after mount — a bare `?area=budget` dropped the org and landed at the top (D12).
    const [governHref, setGovernHref] = useState("?area=budget&focus=monthly_cap");
    useEffect(() => {
      setGovernHref(consoleHref(window.location.search, { area: "budget", focus: "monthly_cap" }));
    }, []);
    const collapsed = mode === "collapsed" && !open && !focused;
    const ratio = budget.cap_usd ? Math.min(1, budget.spent_usd / budget.cap_usd) : 0;
    const period = budget.period === "day" ? "today" : "this month";
    const save = async () => {
      setBusy(true);
      try {
        apply(await setupWrite<SetupView>(projectId, "budget", {
          monthly_cap_usd: Number(value) }, { label: "Save" }));
        toast("Budget saved");
        setEditing(false);
      } catch (error) {
        toast((error as SetupFailure).refusal?.headline ?? "Not saved", "error");
      } finally { setBusy(false); }
    };
    return (
      <SetupRow ref={ref} row="budget" label="Budget" mode={collapsed ? "collapsed" : "open"}
                focused={focused} tip="Applies to metered keys only. Change any time.">
        {collapsed ? (
          <div className="su-line">
            <Tip tip="Applies to metered keys only. Change any time." className="su-faint">
              {usd(budget.monthly_cap_usd ?? budget.cap_usd)} / month
            </Tip>
            <span className="su-chip">Defaults</span>
            <GatedButton permission="budget:manage" type="button" className="su-link" data-testid="su-budget-change"
                    onClick={() => setOpen(true)}>Change</GatedButton>
          </div>
        ) : (
          <div className="su-line" data-testid="su-budget">
            <span data-testid="su-budget-amount">{usd(budget.spent_usd)} of {usd(budget.cap_usd)} {period}</span>
            <span className="su-bar" aria-hidden="true">
              <span className={`su-bar-fill su-tone-${budget.pill.tone}`}
                    style={{ width: `${Math.round(ratio * 100)}%` }} />
            </span>
            <Pill pill={budget.pill} testid="su-budget-pill" />
            {editing ? (
              <>
                <label className="su-field su-field-inline">
                  <span className="su-field-label">Monthly cap ($)</span>
                  <input type="number" min="1" step="1" value={value} aria-label="Monthly cap ($)"
                         data-testid="su-budget-input" onChange={(e) => setValue(e.target.value)} />
                </label>
                <GatedButton permission="budget:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                        disabled={busy || !(Number(value) > 0)} data-testid="su-budget-save"
                        onClick={save}>Save</GatedButton>
              </>
            ) : (
              <GatedButton permission="budget:manage" type="button" className="su-link" data-testid="su-budget-edit"
                      onClick={() => setEditing(true)}>Edit</GatedButton>
            )}
            <a className="su-link" href={governHref} data-testid="su-govern">Govern ↗</a>
          </div>
        )}
      </SetupRow>
    );
  });
