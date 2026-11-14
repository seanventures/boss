"""Text conversions shared by ticket connectors: Atlassian Document Format and HTML to
markdown, markdown to ADF/HTML (for comments), and acceptance criteria from a body."""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Any, Optional

# ---------------------------------------------------------------------------------------------
# ADF -> markdown
# ---------------------------------------------------------------------------------------------


def _marks(text: str, marks: list[dict[str, Any]]) -> str:
    for mark in marks or []:
        kind = mark.get("type")
        if kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "code":
            text = f"`{text}`"
        elif kind == "strike":
            text = f"~~{text}~~"
        elif kind == "link":
            href = (mark.get("attrs") or {}).get("href") or ""
            text = f"[{text}]({href})" if href else text
    return text


def _inline(nodes: list[dict[str, Any]]) -> str:
    out = []
    for node in nodes or []:
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        if kind == "text":
            out.append(_marks(str(node.get("text") or ""), node.get("marks") or []))
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            out.append(str(attrs.get("text") or "@someone"))
        elif kind == "emoji":
            out.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            out.append(str(attrs.get("url") or ""))
        elif kind == "status":
            out.append(f"[{attrs.get('text') or ''}]")
        elif kind == "date":
            out.append(str(attrs.get("timestamp") or ""))
        elif node.get("content"):
            out.append(_inline(node["content"]))
    return "".join(out)


def _block(node: dict[str, Any], depth: int = 0) -> str:
    kind = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}
    if kind == "paragraph":
        return _inline(content)
    if kind == "heading":
        return "#" * int(attrs.get("level") or 1) + " " + _inline(content)
    if kind in ("bulletList", "orderedList", "taskList"):
        lines = []
        for index, item in enumerate(content, start=1):
            if kind == "orderedList":
                bullet = f"{index}."
            elif item.get("type") == "taskItem":
                bullet = "- [x]" if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ]"
            else:
                bullet = "-"
            inner = item.get("content") or []
            if item.get("type") == "taskItem":
                text = _inline(inner)
            else:
                text = "\n".join(_block(child, depth + 1) for child in inner)
            first, *rest = (text or "").split("\n")
            lines.append("  " * depth + f"{bullet} {first}".rstrip())
            lines.extend(rest_line for rest_line in rest if rest_line.strip())
        return "\n".join(lines)
    if kind == "codeBlock":
        language = attrs.get("language") or ""
        return f"```{language}\n{_inline(content)}\n```"
    if kind == "blockquote":
        inner = "\n\n".join(_block(child, depth) for child in content)
        return "\n".join("> " + line for line in inner.split("\n"))
    if kind == "rule":
        return "---"
    if kind in ("panel", "expand", "nestedExpand", "layoutSection", "layoutColumn", "doc"):
        title = attrs.get("title")
        body = "\n\n".join(b for b in (_block(child, depth) for child in content) if b)
        return (f"**{title}**\n\n{body}" if title else body)
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return "[attachment]"
    if kind == "table":
        rows = []
        for row in content:
            cells = [_block_text(cell).replace("\n", " ") for cell in row.get("content") or []]
            rows.append("| " + " | ".join(cells) + " |")
            if len(rows) == 1:
                rows.append("|" + "---|" * len(cells))
        return "\n".join(rows)
    return _inline(content) if content else str(node.get("text") or "")


def _block_text(node: dict[str, Any]) -> str:
    return "\n".join(_block(child) for child in node.get("content") or [])


def adf_to_markdown(adf: Any) -> str:
    """Atlassian Document Format (Jira Cloud v3 bodies and comments) to markdown."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return str(adf)
    if adf.get("type") == "doc":
        blocks = [_block(child) for child in adf.get("content") or []]
        return "\n\n".join(b for b in blocks if b.strip()).strip()
    return _block(adf).strip()


# ---------------------------------------------------------------------------------------------
# markdown -> ADF (comments BOSS writes into Jira)
# ---------------------------------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_BULLET = re.compile(r"^\s*[-*] ")


def _adf_inline(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    pattern = re.compile(f"{_LINK.pattern}|{_BOLD.pattern}|{_CODE.pattern}")
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            nodes.append({"type": "text", "text": text[position:match.start()]})
        if match.group(1) is not None:
            nodes.append({"type": "text", "text": match.group(1),
                          "marks": [{"type": "link", "attrs": {"href": match.group(2)}}]})
        elif match.group(3) is not None:
            nodes.append({"type": "text", "text": match.group(3), "marks": [{"type": "strong"}]})
        else:
            nodes.append({"type": "text", "text": match.group(4), "marks": [{"type": "code"}]})
        position = match.end()
    if position < len(text):
        nodes.append({"type": "text", "text": text[position:]})
    return [n for n in nodes if n.get("text")]


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """A small, safe markdown subset to ADF: paragraphs, bullets, headings, fenced code, links."""
    content: list[dict[str, Any]] = []
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    index = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            inline: list[dict[str, Any]] = []
            for i, line in enumerate(paragraph):
                if i:
                    inline.append({"type": "hardBreak"})
                inline.extend(_adf_inline(line))
            if inline:
                content.append({"type": "paragraph", "content": inline})
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            flush()
            language = line[3:].strip()
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            block: dict[str, Any] = {"type": "codeBlock",
                                     "content": [{"type": "text", "text": "\n".join(code)}]
                                     if code else []}
            if language:
                block["attrs"] = {"language": language}
            content.append(block)
        elif re.match(r"^\s*[-*] ", line):
            flush()
            items = []
            while index < len(lines) and re.match(r"^\s*[-*] ", lines[index]):
                text = re.sub(r"^\s*[-*] ", "", lines[index])
                items.append({"type": "listItem", "content": [
                    {"type": "paragraph", "content": _adf_inline(text) or
                     [{"type": "text", "text": " "}]}]})
                index += 1
            content.append({"type": "bulletList", "content": items})
            continue
        elif re.match(r"^#{1,6} ", line):
            flush()
            level = len(line) - len(line.lstrip("#"))
            content.append({"type": "heading", "attrs": {"level": level},
                            "content": _adf_inline(line[level:].strip())})
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
        index += 1
    flush()
    return {"type": "doc", "version": 1, "content": content or [
        {"type": "paragraph", "content": []}]}


# ---------------------------------------------------------------------------------------------
# HTML <-> markdown (Plane bodies and comments)
# ---------------------------------------------------------------------------------------------


class _HtmlToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list[Any]] = []    # [kind, counter]
        self.href: list[Optional[str]] = []
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if tag in ("p", "div"):
            if not self.lists:            # a paragraph inside a list item stays on its line
                self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("s", "del", "strike"):
            self.out.append("~~")
        elif tag == "code" and not self.in_pre:
            self.out.append("`")
        elif tag == "pre":
            self.in_pre = True
            self.out.append("\n\n```\n")
        elif tag == "a":
            self.href.append(a.get("href"))
            self.out.append("[")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                bullet = f"{self.lists[-1][1]}."
            else:
                bullet = "-"
            if a.get("data-checked") is not None or a.get("data-type") == "taskItem":
                bullet = "- [x]" if a.get("data-checked") == "true" else "- [ ]"
            self.out.append("\n" + "  " * depth + bullet + " ")
        elif tag == "input" and a.get("type") == "checkbox":
            self.out.append("[x] " if "checked" in a else "[ ] ")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag == "img":
            self.out.append(f"![{a.get('alt') or 'image'}]({a.get('src') or ''})")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("s", "del", "strike"):
            self.out.append("~~")
        elif tag == "code" and not self.in_pre:
            self.out.append("`")
        elif tag == "pre":
            self.in_pre = False
            self.out.append("\n```\n\n")
        elif tag == "a":
            href = self.href.pop() if self.href else None
            self.out.append(f"]({href})" if href else "]")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.out.append("\n")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            self.out.append("\n\n" if not self.lists else "")

    def handle_data(self, data: str) -> None:
        if not self.in_pre:
            data = re.sub(r"\s+", " ", data)
        self.out.append(data)


def html_to_markdown(html: str) -> str:
    if not html:
        return ""
    parser = _HtmlToMarkdown()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def markdown_to_html(markdown: str) -> str:
    """Plain, escaped HTML for a markdown comment (paragraphs, bullets, code, links, bold)."""
    blocks = []
    for chunk in re.split(r"\n{2,}", (markdown or "").strip()):
        lines = chunk.split("\n")
        if chunk.startswith("```"):
            body = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            blocks.append(f"<pre><code>{html_lib.escape(body)}</code></pre>")
        elif all(re.match(r"^\s*[-*] ", lb) for lb in lines if lb.strip()):
            items = "".join("<li>" + _html_inline(_BULLET.sub("", lb)) + "</li>"
                            for lb in lines if lb.strip())
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append("<p>" + "<br/>".join(_html_inline(lb) for lb in lines) + "</p>")
    return "".join(blocks) or "<p></p>"


def _html_inline(text: str) -> str:
    text = html_lib.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{html_lib.escape(m.group(2))}">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _CODE.sub(r"<code>\1</code>", text)


# ---------------------------------------------------------------------------------------------
# Acceptance criteria
# ---------------------------------------------------------------------------------------------

_AC_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?\s*(acceptance\s+criteria|definition\s+of\s+done|"
    r"\bac\b)\s*:?\s*(?:\*\*)?\s*:?\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*:?\s*$)")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
_GHERKIN = re.compile(r"^\s*(given|when|then|and)\b.*", re.IGNORECASE)


def acceptance_criteria(markdown: str, field_value: Any = None) -> list[str]:
    """The criteria list: a configured field's value first, else the items under an
    'Acceptance criteria' (or 'Definition of done') heading, else Given/When/Then lines."""
    if field_value not in (None, "", [], {}):
        if isinstance(field_value, list):
            return [str(v).strip() for v in field_value if str(v).strip()]
        text = adf_to_markdown(field_value) if isinstance(field_value, dict) else str(field_value)
        items = [m.group(1) for m in (_ITEM.match(lb) for lb in text.split("\n")) if m]
        return items or [lb.strip() for lb in text.split("\n") if lb.strip()]
    lines = (markdown or "").split("\n")
    for index, line in enumerate(lines):
        if _AC_HEADING.match(line):
            items: list[str] = []
            for after in lines[index + 1:]:
                if _ANY_HEADING.match(after) and not _ITEM.match(after):
                    break
                match = _ITEM.match(after)
                if match:
                    items.append(match.group(1).strip())
                elif after.strip() and _GHERKIN.match(after):
                    items.append(after.strip())
            if items:
                return items
    gherkin = [lb.strip() for lb in lines if _GHERKIN.match(lb) and len(lb.split()) > 2]
    if any(g.lower().startswith("given") for g in gherkin) and \
            any(g.lower().startswith("then") for g in gherkin):
        return gherkin
    return []
