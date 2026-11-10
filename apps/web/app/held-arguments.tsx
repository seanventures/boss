import React from "react";

/**
 * The arguments a held tool call was shown with (recommendation.args_preview), plus the
 * digest of that call. Secret values are already the exact string "[redacted]".
 */

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value == null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}

export function HeldArguments({ recommendation }: { recommendation: Record<string, unknown> }) {
  const preview = recommendation.args_preview;
  if (!isPlainObject(preview)) return null;
  const digest = typeof recommendation.args_sha256 === "string" ? recommendation.args_sha256 : "";
  return (
    <section className="ap">
      <dl data-testid="held-arguments">
        {Object.entries(preview).map(([key, value]) => (
          <div className="ap-row" key={key}>
            <dt>{key}</dt>
            <dd>{asText(value)}</dd>
          </div>
        ))}
        {digest ? (
          <div className="ap-row">
            <dt>sha256</dt>
            <dd data-testid="held-args-sha">{digest}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}
