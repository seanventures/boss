"use client";

/**
 * A redacted JSON object, rendered as data: keys, strings and `[redacted]` values coloured,
 * built from React text nodes only (never parsed back into HTML), so nothing in a stored event
 * can ever become markup on this page.
 */
import React, { type ReactNode } from "react";

function value(v: unknown, indent: string): ReactNode {
  if (typeof v === "string") {
    const redacted = /^\[redacted\]$/i.test(v);
    return <span className={redacted ? "r" : "s"}>{JSON.stringify(v)}</span>;
  }
  if (v === null || typeof v === "number" || typeof v === "boolean") return <span>{String(v)}</span>;
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    const inner = indent + "  ";
    return (
      <>
        {"["}
        {v.map((item, i) => (
          <React.Fragment key={i}>{"\n"}{inner}{value(item, inner)}{i < v.length - 1 ? "," : ""}</React.Fragment>
        ))}
        {"\n"}{indent}{"]"}
      </>
    );
  }
  if (typeof v === "object") return object(v as Record<string, unknown>, indent);
  return <span>{String(v)}</span>;
}

function object(o: Record<string, unknown>, indent: string): ReactNode {
  const keys = Object.keys(o);
  if (!keys.length) return "{}";
  const inner = indent + "  ";
  return (
    <>
      {"{"}
      {keys.map((k, i) => (
        <React.Fragment key={k}>
          {"\n"}{inner}<span className="k">{JSON.stringify(k)}</span>{": "}{value(o[k], inner)}{i < keys.length - 1 ? "," : ""}
        </React.Fragment>
      ))}
      {"\n"}{indent}{"}"}
    </>
  );
}

export function JsonView({ data, label }: { data: Record<string, unknown>; label: string }) {
  return (
    <pre className="idn-json" aria-label={label} data-copy="data" data-testid="json-view" tabIndex={0}>
      {object(data, "")}
    </pre>
  );
}
