"use client";

/**
 * THE ONE FORM KIT (BOSSAPP-1039). Five wave-4 surfaces each hand-rolled their small add/edit
 * form: browser-default white inputs on the dark theme, checkboxes half a row away from their
 * labels, a form dropped into a stray grid cell away from its row. Every compact form builds
 * from these parts instead. They render the console's established classes — globals.css's
 * `.field` / `.input` / `.check-row`, themed by design-system.css — so the type scale and the
 * tokens are exactly the Workflows/Govern ones; the kit adds only the `.fk-*` layout rules
 * (globals.css, tokens only).
 */
import React from "react";

/** The form itself: a bordered panel of compact fields, full width inside any row or drawer. */
export function FormGrid({ label, testId, children }: {
  label: string; testId?: string; children: React.ReactNode;
}) {
  return (
    <div className="fk-form" role="group" aria-label={label} data-testid={testId}>
      {children}
    </div>
  );
}

/**
 * One labelled control. The control is the child; the kit gives it the console's `.input`
 * (or `.textarea`) look here, so no screen styles an input on its own again.
 */
export function Field({ label, tip, wide, children }: {
  label: React.ReactNode; tip?: string; wide?: boolean; children: React.ReactElement;
}) {
  const child = React.Children.only(children) as React.ReactElement<{ className?: string }>;
  const base = child.type === "textarea" ? "textarea" : "input";
  const control = React.cloneElement(child, {
    className: child.props.className ? `${base} ${child.props.className}` : base,
  });
  return (
    <label className={wide ? "field fk-wide" : "field"} title={tip}>
      <span>{label}</span>
      {control}
    </label>
  );
}

/** A checkbox ON its words: the label wraps the box, so clicking the words toggles it. */
export function CheckRow({ label, checked, onChange, tip, testId, disabled }: {
  label: React.ReactNode; checked: boolean; onChange: () => void;
  tip?: string; testId?: string; disabled?: boolean;
}) {
  return (
    <label className="check-row" title={tip}>
      <input type="checkbox" checked={checked} onChange={onChange} data-testid={testId} disabled={disabled} />
      <span>{label}</span>
    </label>
  );
}

/** A named group of CheckRows, laid out as a wrapping row under one label. */
export function CheckGroup({ label, tip, testId, children }: {
  label: React.ReactNode; tip?: string; testId?: string; children: React.ReactNode;
}) {
  return (
    <fieldset className="field fk-wide" aria-label={typeof label === "string" ? label : undefined}
              title={tip} data-testid={testId}>
      <span>{label}</span>
      <span className="fk-checks">{children}</span>
    </fieldset>
  );
}

/** What stopped the save, under the fields (red, role=alert; never a silent form). */
export function FormError({ children }: { children: React.ReactNode }) {
  if (!children) return null;
  return <p className="fk-err fk-wide" role="alert">{children}</p>;
}
