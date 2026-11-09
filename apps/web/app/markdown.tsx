import React from "react";

// Dependency-free markdown renderer for the subset our agent/meeting prose uses:
// headings, bold/italic, inline code, links, ordered/unordered lists, code
// fences, horizontal rules, and paragraphs. Good enough to make LLM output read
// cleanly without pulling in a markdown library.

/**
 * The only hrefs this renderer will make: http(s), and same-origin paths and anchors. Whatever
 * a pre-pass did, the renderer is safe on its own, text an operator uploaded (an agent's
 * instructions) or a model wrote is rendered here, and `[x](javascript:…)` is a script.
 */
export function safeHref(href: string): boolean {
  const h = href.trim();
  if (/^https?:\/\/[^\s]/i.test(h)) return true;
  if (h.startsWith("//")) return false;
  return /^[/#]/.test(h) && !/[\u0000-\u001f\\]/.test(h);
}

/** Plain text (and an inline code span, `code: true`) handed to the caller before it is shown:
 *  the conversation view turns the ids in an answer into chips. Returns what to render. */
export type InlineText = (text: string, key: string, code?: boolean) => React.ReactNode;

function renderInline(text: string, keyPrefix: string, hook?: InlineText): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const plain = (t: string, k: string) => nodes.push(hook ? <React.Fragment key={k}>{hook(t, k)}</React.Fragment> : t);
  // UNDERSCORES INSIDE A WORD ARE NOT EMPHASIS (CommonMark's rule). Without the lookarounds
  // `BOSS_AGENT_HOST_SHELL` rendered as "BOSSAGENTHOSTSHELL" and `run_on_host` as "runonhost" , 
  // an operator was told to set a setting whose name the console had just destroyed.
  const pattern =
    /(`[^`]+`)|(\*\*[^*]+\*\*|(?<![A-Za-z0-9_])__[^_]+__(?![A-Za-z0-9_]))|(\*[^*\s][^*]*\*|(?<![A-Za-z0-9_])_[^_\s][^_]*_(?![A-Za-z0-9_]))|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) plain(text.slice(last, m.index), `${keyPrefix}-t${i}`);
    const tok = m[0];
    const key = `${keyPrefix}-${i}`;
    if (tok.startsWith("`")) {
      nodes.push(hook ? <React.Fragment key={key}>{hook(tok.slice(1, -1), key, true)}</React.Fragment>
        : <code key={key}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      nodes.push(<strong key={key}>{hook ? hook(tok.slice(2, -2), key) : tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("[")) {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok);
      if (mm && safeHref(mm[2])) {
        nodes.push(
          <a key={key} href={mm[2]} target="_blank" rel="noopener noreferrer">
            {mm[1]}
          </a>,
        );
      } else if (mm) {
        // Not a link we will make: javascript:, data:, a protocol-relative host… shown as text.
        nodes.push(mm[1]);
      } else {
        nodes.push(tok);
      }
    } else {
      nodes.push(<em key={key}>{hook ? hook(tok.slice(1, -1), key) : tok.slice(1, -1)}</em>);
    }
    last = m.index + tok.length;
    i++;
  }
  if (last < text.length) plain(text.slice(last), `${keyPrefix}-t${i}`);
  return nodes;
}

const SPECIAL = /^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|```)/;
const HR = /^(-{3,}|\*{3,}|_{3,})$/;

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// A GFM table separator row: |---|:--:|---| (cells of dashes, optional colons).
function isTableSeparator(line: string): boolean {
  const t = line.trim();
  if (!t.includes("|") || !t.includes("-")) return false;
  const cells = t.replace(/^\|/, "").replace(/\|$/, "").split("|");
  return cells.length >= 1 && cells.every((c) => /^\s*:?-{1,}:?\s*$/.test(c));
}

export function Markdown({
  text,
  className,
  inline,
}: {
  text?: string | null;
  className?: string;
  /** See InlineText. */
  inline?: InlineText;
}) {
  if (!text) return null;
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (line.trim().startsWith("```")) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        buf.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      blocks.push(
        <pre key={k++} className="md-pre">
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = Math.min(h[1].length + 1, 6);
      const Tag = `h${level}` as React.ElementType;
      const hid = k++;
      blocks.push(
        <Tag key={hid} className="md-h">
          {renderInline(h[2], `h${hid}`, inline)}
        </Tag>,
      );
      i++;
      continue;
    }

    if (HR.test(line.trim())) {
      blocks.push(<hr key={k++} className="md-hr" />);
      i++;
      continue;
    }

    // GFM table: a header row containing "|" followed by a separator row.
    if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const header = splitRow(line);
      i += 2; // skip header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim() !== "" && lines[i].includes("|")) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const tk = k++;
      blocks.push(
        <div key={tk} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {header.map((h, ci) => (
                  <th key={ci}>{renderInline(h, `th${tk}-${ci}`, inline)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, ci) => (
                    <td key={ci}>{renderInline(c, `td${tk}-${ri}-${ci}`, inline)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(
          <li key={items.length}>
            {renderInline(lines[i].replace(/^\s*[-*+]\s+/, ""), `ul${k}-${items.length}`, inline)}
          </li>,
        );
        i++;
      }
      blocks.push(
        <ul key={k++} className="md-ul">
          {items}
        </ul>,
      );
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: React.ReactNode[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(
          <li key={items.length}>
            {renderInline(lines[i].replace(/^\s*\d+[.)]\s+/, ""), `ol${k}-${items.length}`, inline)}
          </li>,
        );
        i++;
      }
      blocks.push(
        <ol key={k++} className="md-ol">
          {items}
        </ol>,
      );
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !SPECIAL.test(lines[i]) &&
      !HR.test(lines[i].trim())
    ) {
      para.push(lines[i]);
      i++;
    }
    const pid = k++;
    blocks.push(
      <p key={pid} className="md-p">
        {renderInline(para.join(" "), `p${pid}`, inline)}
      </p>,
    );
  }
  return <div className={`md${className ? " " + className : ""}`}>{blocks}</div>;
}
