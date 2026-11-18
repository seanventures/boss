"""Confluence storage format (XHTML with ac:/ri: macros) <-> markdown.

Reading: pages come back as storage-format XHTML. They are turned into clean markdown — headings,
lists, links, tables, code, task lists and info panels — with every ordinary link preserved. A
macro with no text of its own (table of contents, children display) is dropped; a page link
(``<ac:link><ri:page ri:content-title=.../>``) becomes ``[Title](confluence-page:<url-quoted Title>)`` so the
reference survives; an attachment image becomes ``![name](attachment:name)``.

Writing: markdown is turned into storage format — escaped text, never raw HTML passed through.
Fenced code becomes the ``code`` macro, so Confluence renders it as a code block.
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import quote
from typing import Any, Optional

from ..tickets.text import _HtmlToMarkdown

_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_PANELS = {"info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip", "panel": "Panel"}


class _StorageToMarkdown(_HtmlToMarkdown):
    def __init__(self) -> None:
        super().__init__()
        self.macros: list[str] = []
        self.param: Optional[str] = None
        self.language = ""
        self.links: list[dict[str, Any]] = []
        self.cells = 0
        self.header_row = False
        self.skip = 0                       # inside a macro whose body must not be printed
        self.task: Optional[dict[str, Any]] = None

    # -- helpers ------------------------------------------------------------------------------

    def _macro(self) -> str:
        return self.macros[-1] if self.macros else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if self.skip:
            if tag == "ac:structured-macro":
                self.skip += 1
            return
        if tag == "ac:structured-macro":
            name = str(a.get("ac:name") or "").lower()
            self.macros.append(name)
            self.language = ""
            if name in ("toc", "children", "recently-updated", "pagetree", "excerpt-include",
                        "include", "jira", "status", "anchor", "contentbylabel"):
                self.skip = 1
            elif name in _PANELS:
                self.out.append(f"\n\n> **{_PANELS[name]}:** ")
            return
        if tag == "ac:parameter":
            self.param = str(a.get("ac:name") or "")
            return
        if tag == "ac:plain-text-body":
            if self._macro() == "code":
                self.in_pre = True
                self.out.append(f"\n\n```{self.language}\n")
            return
        if tag in ("ac:rich-text-body", "ac:layout", "ac:layout-section", "ac:layout-cell",
                   "ac:inline-comment-marker"):
            return
        if tag == "ac:link":
            self.links.append({"title": "", "body": "", "anchor": a.get("ac:anchor")})
            return
        if tag == "ri:page" and self.links:
            self.links[-1]["title"] = str(a.get("ri:content-title") or "")
            return
        if tag == "ri:attachment":
            if self.links:
                self.links[-1]["title"] = str(a.get("ri:filename") or "")
            self.last_attachment = str(a.get("ri:filename") or "")
            return
        if tag == "ri:user":
            self.out.append("@user")
            return
        if tag == "ac:link-body" or tag == "ac:plain-text-link-body":
            self.link_body = True
            return
        if tag == "ac:image":
            self.last_attachment = ""
            self.image_alt = str(a.get("ac:alt") or "")
            self.in_image = True
            return
        if tag == "ac:task":
            self.task = {"done": False}
            return
        if tag == "ac:task-status" or tag == "ac:task-body":
            self.task_part = tag
            return
        if tag == "ac:emoticon":
            return
        if tag == "time":
            self.out.append(str(a.get("datetime") or ""))
            return
        if tag == "table":
            self.out.append("\n\n")
            return
        if tag == "tr":
            self.cells = 0
            self.header_row = False
            self.out.append("\n|")
            return
        if tag in ("th", "td"):
            self.header_row = self.header_row or tag == "th"
            self.out.append(" ")
            return
        if tag == "ac:task-list":
            return
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.skip:
            if tag == "ac:structured-macro":
                self.skip -= 1
                if not self.skip and self.macros:
                    self.macros.pop()
            return
        if tag == "ac:structured-macro":
            if self.macros:
                name = self.macros.pop()
                if name in _PANELS:
                    self.out.append("\n\n")
            return
        if tag == "ac:parameter":
            self.param = None
            return
        if tag == "ac:plain-text-body":
            if self.in_pre:
                self.in_pre = False
                self.out.append("\n```\n\n")
            return
        if tag == "ac:link":
            link = self.links.pop() if self.links else {"title": "", "body": ""}
            label = link.get("body") or link.get("title") or ""
            if label:
                target = link["title"] or label
                self.out.append(f"[{label}](confluence-page:{quote(target)})")
            return
        if tag in ("ac:link-body", "ac:plain-text-link-body"):
            self.link_body = False
            return
        if tag == "ac:image":
            self.in_image = False
            name = getattr(self, "last_attachment", "")
            if name:
                self.out.append(f"![{getattr(self, 'image_alt', '') or name}](attachment:{name})")
            return
        if tag == "ac:task":
            task, self.task = self.task or {}, None
            self.out.append(f"\n- [{'x' if task.get('done') else ' '}] {task.get('body', '').strip()}")
            return
        if tag in ("ac:task-status", "ac:task-body"):
            self.task_part = ""
            return
        if tag in ("th", "td"):
            self.cells += 1
            self.out.append(" |")
            return
        if tag == "tr":
            if self.header_row:
                self.out.append("\n|" + " --- |" * max(self.cells, 1))
            return
        if tag in ("table", "ac:task-list"):
            self.out.append("\n\n")
            return
        if tag in ("ac:rich-text-body", "ac:layout", "ac:layout-section", "ac:layout-cell",
                   "ac:inline-comment-marker", "ri:page", "ri:attachment", "ri:user",
                   "ac:emoticon", "time", "thead", "tbody"):
            return
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.param is not None:
            if self.param == "language" and self._macro() == "code":
                self.language = data.strip()
            return
        if self.task is not None:
            part = getattr(self, "task_part", "")
            if part == "ac:task-status":
                self.task["done"] = data.strip() == "complete"
            elif part == "ac:task-body":
                self.task["body"] = self.task.get("body", "") + re.sub(r"\s+", " ", data)
            return
        if self.links and getattr(self, "link_body", False):
            self.links[-1]["body"] += data
            return
        if getattr(self, "in_image", False):
            return
        super().handle_data(data)


def storage_to_markdown(storage: str) -> str:
    """Clean markdown from a page's storage-format body. Never raises on odd markup."""
    if not storage:
        return ""
    prepared = _CDATA.sub(lambda m: html_lib.escape(m.group(1), quote=False), storage)
    parser = _StorageToMarkdown()
    try:
        parser.feed(prepared)
        parser.close()
    except Exception:  # noqa: BLE001 — a malformed page still yields its text
        pass
    text = "".join(parser.out)
    text = re.sub(r"(> \*\*(?:Info|Note|Warning|Tip|Panel):\*\*)\s*\n+", r"\1 ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------------------------
# markdown -> storage format
# ---------------------------------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_CODE = re.compile(r"`([^`]+)`")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_SAFE_HREF = re.compile(r"^(https?://|mailto:|/|#)", re.IGNORECASE)


def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(f"<code>{html_lib.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(keep, text)
    text = html_lib.escape(text, quote=False)

    def link(m: re.Match) -> str:
        href = html_lib.unescape(m.group(2))
        if not _SAFE_HREF.match(href):
            return m.group(1)
        return f'<a href="{html_lib.escape(href, quote=True)}">{m.group(1)}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _code_macro(lines: list[str], language: str) -> str:
    body = "\n".join(lines).replace("]]>", "]]]]><![CDATA[>")
    lang = (f'<ac:parameter ac:name="language">{html_lib.escape(language, quote=True)}'
            "</ac:parameter>") if language else ""
    return (f'<ac:structured-macro ac:name="code" ac:schema-version="1">{lang}'
            f"<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>")


def _list_html(items: list[tuple[int, str, str]]) -> str:
    """Nested ul/ol from (indent, marker kind, text) rows."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, kind, body in items:
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0] and kind != stack[-1][1]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            stack.append((indent, kind))
            out.append(f"<{kind}><li>{_inline(body)}")
        else:
            out.append(f"</li><li>{_inline(body)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_storage(markdown: str) -> str:
    """Storage-format XHTML for a markdown body. Text is escaped; links keep only http(s),
    mailto and relative targets."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    i = 0

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br/>".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            language = stripped[3:].strip().split()[0] if stripped[3:].strip() else ""
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(_code_macro(body, language))
            i += 1
            continue
        if not stripped:
            flush()
            i += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _RULE.match(stripped):
            flush()
            out.append("<hr/>")
            i += 1
            continue
        if "|" in stripped and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head = _cells(stripped)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            html = "<table><tbody><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
            html += "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                            for r in rows)
            out.append(html + "</tbody></table>")
            continue
        if stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>" + "<br/>".join(_inline(q) for q in quote) + "</p></blockquote>")
            continue
        if _BULLET.match(line):
            flush()
            items: list[tuple[int, str, str]] = []
            while i < len(lines) and _BULLET.match(lines[i]):
                m = _BULLET.match(lines[i])
                items.append((len(m.group(1).replace("\t", "    ")),
                              "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)))
                i += 1
            out.append(_list_html(items))
            continue
        paragraph.append(stripped)
        i += 1
    flush()
    return "".join(out) or "<p></p>"
