import React from "react";

/**
 * WHAT APPROVING WILL DO, drawn as the thing itself.
 *
 * Found running the bundled examples live: a gate on "Email the newsletter" showed a workflow
 * key, a run id and "staging" — not who the email went to or what it said. The engine now
 * resolves the step's real inputs when the gate opens and stores a typed preview on the decision
 * (engine/boss_engine/services/action_preview.py). This draws it: an email as an email, tickets
 * as a list, a publish as a small card, anything else as labelled fields. Labels and tooltips,
 * no paragraphs.
 */

export type EmailPreview = {
  kind: "email"; to: string[]; cc?: string[]; subject: string | null; body: string;
  body_truncated?: boolean; attachments: string[];
};
export type IssuesPreview = {
  kind: "issues"; count: number; tracker?: string | null;
  items: { title: string; owner: string | null; excerpt: string; due?: string | null }[];
};
export type PublishPreview = {
  kind: "publish"; platform: string; title: string | null; description: string;
  visibility: string; tags?: string[];
  media: { id: string | null; name: string | null; link: string | null };
};
export type FieldsPreview = {
  kind: "fields";
  /** A release card: the first row is the what-ships sentence and the rest are commits. */
  release?: boolean;
  fields: { name?: string; label: string; value: string; redacted?: boolean; truncated?: boolean }[];
};
export type UnresolvedPreview = { kind: "unresolved"; reason: string };

export type ActionPreviewData = (EmailPreview | IssuesPreview | PublishPreview | FieldsPreview
  | UnresolvedPreview) & {
  skill_id?: string | null; step_label?: string | null; content_hash?: string | null;
  redactions?: number;
};

const KINDS = new Set(["email", "issues", "publish", "fields", "unresolved"]);

/** The preview on a decision's recommendation or a step, or null when there is none. */
export function actionPreviewOf(value: unknown): ActionPreviewData | null {
  if (!value || typeof value !== "object") return null;
  const kind = (value as { kind?: unknown }).kind;
  return typeof kind === "string" && KINDS.has(kind) ? (value as ActionPreviewData) : null;
}

/** One short line saying what pressing Approve does with what is shown below it. */
export function previewLead(preview: ActionPreviewData): string {
  switch (preview.kind) {
    case "email": return "Approving sends this email.";
    case "issues":
      return `Approving creates ${preview.count === 1 ? "this ticket" : `these ${preview.count} tickets`}`
        + `${preview.tracker ? ` in ${preview.tracker}` : ""}.`;
    case "publish": return `Approving publishes this to ${preview.platform}.`;
    case "unresolved": return "Approving runs the step; its inputs are not ready to show yet.";
    default: return "Approving runs the step with these inputs.";
  }
}

function Row({ label, tip, children, testId }: {
  label: string; tip?: string; children: React.ReactNode; testId?: string;
}) {
  return (
    <div className="ap-row" data-testid={testId}>
      <dt title={tip}>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function EmailCard({ p }: { p: EmailPreview }) {
  return (
    <div className="ap-email" data-testid="action-preview-email">
      <dl className="ap-head">
      <Row label="To" tip="Who receives it" testId="ap-to">{p.to.join(", ") || "—"}</Row>
      {p.cc && p.cc.length ? <Row label="Cc">{p.cc.join(", ")}</Row> : null}
      <Row label="Subject" testId="ap-subject">{p.subject || "(no subject)"}</Row>
      {p.attachments.length ? (
        <Row label="Attached" tip="Files sent with it">{p.attachments.join(", ")}</Row>
      ) : null}
      </dl>
      <div className="ap-body" data-testid="ap-body" tabIndex={0}
           title="The full text that will be sent">
        {p.body || "(empty)"}
        {p.body_truncated ? <span className="ap-more"> — shortened here</span> : null}
      </div>
    </div>
  );
}

function IssuesList({ p }: { p: IssuesPreview }) {
  return (
    <ol className="ap-issues" data-testid="action-preview-issues">
      {p.items.map((item, i) => (
        <li key={i} title={item.excerpt || undefined}>
          <strong>{item.title}</strong>
          {item.owner ? <span className="ap-chip" title="Owner as named">{item.owner}</span> : null}
          {item.due ? <span className="ap-chip" title="Timing as said">{item.due}</span> : null}
          {item.excerpt ? <span className="ap-excerpt">{item.excerpt}</span> : null}
        </li>
      ))}
      {p.count > p.items.length ? (
        <li className="ap-more">and {p.count - p.items.length} more</li>
      ) : null}
    </ol>
  );
}

function PublishCard({ p }: { p: PublishPreview }) {
  const media = p.media.name || p.media.id || "—";
  return (
    <div className="ap-publish" data-testid="action-preview-publish">
      <dl className="ap-head">
      <Row label="Title" testId="ap-title">{p.title || "(no title)"}</Row>
      <Row label="Visibility" tip="Who can see it once published">{p.visibility}</Row>
      <Row label="File" tip="The edited media that is uploaded">
        {p.media.link ? <a href={p.media.link} target="_blank" rel="noreferrer">{media}</a> : media}
      </Row>
      </dl>
      {p.description ? (
        <div className="ap-body ap-body-short" data-testid="ap-description" tabIndex={0}
             title="The description it is published with">{p.description}</div>
      ) : null}
    </div>
  );
}

function FieldList({ p }: { p: FieldsPreview }) {
  return (
    <dl className="ap-fields" data-testid="action-preview-fields">
      {p.fields.map((f) => (
        <Row key={f.name} label={f.label} tip={f.redacted ? "Hidden: looks like a credential" : f.name}>
          <span className={f.value.length > 80 ? "ap-long" : undefined}>{f.value}</span>
        </Row>
      ))}
    </dl>
  );
}

function ReleaseCommits({ p }: { p: FieldsPreview }) {
  const [head, ...commits] = p.fields;
  return (
    <div data-testid="action-preview-release">
      {head ? (
        <p className="ap-what-ships" data-testid="ap-what-ships">{head.value ?? ""}</p>
      ) : null}
      <ol className="ap-commits" data-testid="ap-commits">
        {commits.map((f, i) => (
          <li key={`${f.label}-${i}`} className="ap-commit" data-testid="ap-commit">
            <span className="ap-commit-id">{f.label}</span>
            <span className="ap-commit-msg">{f.value ?? ""}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function ActionPreview({ preview }: { preview: ActionPreviewData }) {
  return (
    <section className="ap" data-testid="action-preview" data-kind={preview.kind}
             aria-label="What approving will do">
      {preview.kind === "email" ? <EmailCard p={preview} />
        : preview.kind === "issues" ? <IssuesList p={preview} />
        : preview.kind === "publish" ? <PublishCard p={preview} />
        : preview.kind === "fields" && preview.release ? <ReleaseCommits p={preview} />
        : preview.kind === "fields" ? <FieldList p={preview} />
        : <p className="ap-unresolved" title={preview.reason}>Not resolvable yet: {preview.reason}</p>}
      {preview.redactions ? (
        <p className="ap-redacted" data-testid="ap-redacted"
           title="Values that look like keys or passwords are hidden here; they are still sent">
          {preview.redactions} credential-like value{preview.redactions === 1 ? "" : "s"} hidden
        </p>
      ) : null}
    </section>
  );
}
