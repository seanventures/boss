"""Build the BOSS OSS design-partner one-page PDF."""
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "pdf" / "boss-oss-one-pager.pdf"


def text(canvas, value, x, y, size, color, font="Helvetica"):
    canvas.setFont(font, size)
    canvas.setFillColor(color)
    canvas.drawString(x, y, value)


def wrap(canvas, value, x, y, width, size, leading, color, font="Helvetica"):
    words = value.split()
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if stringWidth(candidate, font, size) > width and line:
            text(canvas, line, x, y, size, color, font)
            y -= leading
            line = word
        else:
            line = candidate
    if line:
        text(canvas, line, x, y, size, color, font)
    return y - leading


def build():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(OUT), pagesize=letter)
    width, height = letter
    ink, paper, acid, muted = HexColor("#0a0d0f"), HexColor("#e9ebe5"), HexColor("#d8ff39"), HexColor("#69706b")
    canvas.setFillColor(paper)
    canvas.rect(0, 0, width, height, fill=1, stroke=0)
    canvas.setFillColor(ink)
    canvas.rect(0, height - 250, width, 250, fill=1, stroke=0)
    for x in range(0, int(width), 36):
        canvas.setStrokeColor(HexColor("#263126"))
        canvas.line(x, height - 250, x, height)
    text(canvas, "BOSS", 48, height - 56, 23, acid, "Helvetica-Bold")
    text(canvas, "GOVERNED AI LABOR", 48, height - 76, 7, acid, "Helvetica-Bold")
    text(canvas, "Make AI work", 48, height - 142, 39, paper, "Helvetica-Bold")
    text(canvas, "accountable.", 48, height - 184, 39, paper, "Helvetica-Bold")
    text(canvas, "The operating system for AI labor", 48, height - 217, 10, HexColor("#bec6bc"))
    y = height - 290
    text(canvas, "A directive is not a deliverable.", 48, y, 18, ink, "Helvetica-Bold")
    y -= 25
    y = wrap(canvas, "BOSS turns intent into governed execution with role-scoped work, explicit approvals, budget controls, durable receipts, and evidence you can inspect.", 48, y, 500, 10.5, 14, HexColor("#343a35"))
    y -= 16
    steps = [("01", "INTENT", "A directive enters a governed work queue."), ("02", "EVIDENCE", "Roles, tests, and adversarial review make the case."), ("03", "DECISION", "A human approval is recorded before irreversible work."), ("04", "RECEIPT", "Consequential actions leave an inspectable trail.")]
    col_w = 129
    for i, (number, title, body) in enumerate(steps):
        x = 48 + i * col_w
        text(canvas, number, x, y, 8, HexColor("#6d7c18"), "Helvetica-Bold")
        text(canvas, title, x, y - 24, 11, ink, "Helvetica-Bold")
        wrap(canvas, body, x, y - 42, 112, 8.5, 11, HexColor("#4d554f"))
    y -= 112
    canvas.setFillColor(ink)
    canvas.rect(48, y - 128, width - 96, 128, fill=1, stroke=0)
    text(canvas, "WHAT SHIPS IN THE OSS ENGINE", 66, y - 25, 7, acid, "Helvetica-Bold")
    items = ["Self-hosted engine + durable PostgreSQL workspace", "CLI and MCP interface for external agent clients", "Ollama, vLLM, and OpenAI-compatible local-model lanes", "Read-only community Mission Control", "Receipts, approvals, budgets, and leased jobs"]
    for i, item in enumerate(items):
        text(canvas, f"{i + 1:02d}", 66, y - 49 - i * 15, 7, acid, "Helvetica-Bold")
        text(canvas, item, 88, y - 49 - i * 15, 8.5, paper)
    y -= 158
    text(canvas, "START LOCALLY", 48, y, 8, HexColor("#6d7c18"), "Helvetica-Bold")
    y -= 21
    canvas.setFillColor(HexColor("#1a201d"))
    canvas.roundRect(48, y - 29, 420, 29, 3, fill=1, stroke=0)
    text(canvas, "$ docker build -f engine/Dockerfile -t boss-engine .", 61, y - 18, 8.5, acid, "Courier")
    wrap(canvas, "Connect a self-hosted PostgreSQL workspace, bootstrap once, then drive BOSS from the CLI or an MCP-capable client.", 48, y - 51, 500, 9.5, 13, HexColor("#343a35"))
    text(canvas, "BOSS / ENGINE + CONTROL PLANE / DESIGN PARTNERS", 48, 35, 6.5, muted, "Helvetica-Bold")
    canvas.showPage()
    canvas.save()


if __name__ == "__main__":
    build()
