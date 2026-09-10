"""Render a meeting recap to PDF and/or DOCX for easier reading.

    from export import to_pdf, to_docx
    to_pdf(markdown_text, pathlib.Path("notes.pdf"), title="Meeting notes")

This is a deliberately small markdown renderer, not a general one. It handles
exactly what `notes.RECAP_SYSTEM_PROMPT` produces -- `#`/`##` headings, `-`
bullets, `>` blockquotes, `---` rules, paragraphs, and inline `**bold**` and
`` `code` ``. Anything else falls through as plain text rather than erroring.

A full markdown-to-PDF pipeline (Puppeteer or WeasyPrint) would mean either a
Node toolchain or GTK system libraries on Windows. reportlab and python-docx are
pure Python, so the project stays installable with one `uv sync`.
"""

from __future__ import annotations

import pathlib
import re
from xml.sax.saxutils import escape

# Accent colours for the rendered notes. Change them to your own.
BRAND_BLUE = "#13A4E0"
BRAND_PURPLE = "#A800FF"
INK = "#1A1A1A"
MUTED = "#5A5A5A"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE = re.compile(r"`([^`]+)`")


def _blocks(md: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, text) blocks.

    kind is one of: h1, h2, bullet, quote, rule, para.
    """
    out: list[tuple[str, str]] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(("para", " ".join(para).strip()))
            para.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush()
            continue
        if stripped in {"---", "***", "___"}:
            flush()
            out.append(("rule", ""))
            continue
        if stripped.startswith("## "):
            flush()
            out.append(("h2", stripped[3:].strip()))
            continue
        if stripped.startswith("# "):
            flush()
            out.append(("h1", stripped[2:].strip()))
            continue
        if stripped.startswith("> "):
            flush()
            out.append(("quote", stripped[2:].strip()))
            continue
        if stripped.startswith(("- ", "* ")):
            flush()
            out.append(("bullet", stripped[2:].strip()))
            continue
        para.append(stripped)

    flush()
    # Merge consecutive quote lines into one block so the rendered box holds the
    # whole note rather than one line per paragraph.
    merged: list[tuple[str, str]] = []
    for kind, text in out:
        if kind == "quote" and merged and merged[-1][0] == "quote":
            merged[-1] = ("quote", f"{merged[-1][1]} {text}")
        else:
            merged.append((kind, text))

    # The recap's header block (Attended by / Started / Meeting / ...) is written
    # as bullets, but reads as a field list rather than a list of points. It is
    # always above the first horizontal rule, so relabel those as "meta" and
    # render them without bullet glyphs.
    if any(k == "rule" for k, _ in merged):
        relabelled: list[tuple[str, str]] = []
        seen_rule = False
        for kind, text in merged:
            if kind == "rule":
                seen_rule = True
            elif kind == "bullet" and not seen_rule:
                kind = "meta"
            relabelled.append((kind, text))
        return relabelled
    return merged


def _inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """Split inline text into (text, bold, code) runs, for DOCX."""
    runs: list[tuple[str, bool, bool]] = []
    pos = 0
    pattern = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`", re.DOTALL)
    for m in pattern.finditer(text):
        if m.start() > pos:
            runs.append((text[pos : m.start()], False, False))
        if m.group(1) is not None:
            runs.append((m.group(1), True, False))
        else:
            runs.append((m.group(2), False, True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False, False))
    return runs or [(text, False, False)]


def _to_rl_markup(text: str) -> str:
    """Inline markdown -> reportlab's mini-HTML. Escape first, then add tags, so
    a stray < or & in the transcript can't break the document."""
    s = escape(text)
    s = _BOLD.sub(r"<b>\1</b>", s)
    s = _CODE.sub(r'<font face="Courier" size="9">\1</font>', s)
    return s


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def to_pdf(md: str, path: pathlib.Path, title: str = "Meeting notes") -> pathlib.Path:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    base = getSampleStyleSheet()["BodyText"]
    body = ParagraphStyle(
        "Body", parent=base, fontName="Helvetica", fontSize=10.5, leading=15.5,
        textColor=HexColor(INK), spaceAfter=7, alignment=TA_LEFT,
    )
    h1 = ParagraphStyle(
        "H1", parent=body, fontName="Helvetica-Bold", fontSize=19, leading=23,
        textColor=HexColor(BRAND_BLUE), spaceBefore=0, spaceAfter=12,
    )
    h2 = ParagraphStyle(
        "H2", parent=body, fontName="Helvetica-Bold", fontSize=12.5, leading=16,
        textColor=HexColor(BRAND_PURPLE), spaceBefore=16, spaceAfter=6,
    )
    quote = ParagraphStyle(
        "Quote", parent=body, fontSize=9, leading=13, textColor=HexColor(MUTED),
        leftIndent=10, borderPadding=0, spaceBefore=4, spaceAfter=10,
    )
    bullet = ParagraphStyle("Bullet", parent=body, spaceAfter=3)
    meta = ParagraphStyle(
        "Meta", parent=body, fontSize=10, leading=14.5, leftIndent=2, spaceAfter=4
    )

    # Adjacent bullets are collected and emitted as one ListFlowable, so a list
    # renders tightly instead of as a stack of separate one-item lists.
    grouped: list = []
    pending: list = []

    def flush_pending() -> None:
        if not pending:
            return
        grouped.append(
            ListFlowable(
                [ListItem(p, leftIndent=18) for p in pending],
                bulletType="bullet",
                # An explicit glyph. reportlab's named starts ("circle") render
                # as nothing here, which silently produced indented text with no
                # bullets at all.
                start="•",
                bulletFontName="Helvetica",
                bulletFontSize=10.5,
                leftIndent=18,
                bulletOffsetY=-1,
                spaceBefore=2,
                spaceAfter=8,
            )
        )
        pending.clear()

    for kind, text in _blocks(md):
        if kind == "bullet":
            pending.append(Paragraph(_to_rl_markup(text), bullet))
            continue
        flush_pending()
        if kind == "h1":
            grouped.append(Paragraph(_to_rl_markup(text), h1))
        elif kind == "h2":
            grouped.append(Paragraph(_to_rl_markup(text), h2))
        elif kind == "meta":
            grouped.append(Paragraph(_to_rl_markup(text), meta))
        elif kind == "quote":
            grouped.append(Paragraph(_to_rl_markup(text), quote))
        elif kind == "rule":
            grouped.append(Spacer(1, 4))
            grouped.append(HRFlowable(width="100%", thickness=0.6, color=HexColor("#D8D8D8")))
            grouped.append(Spacer(1, 8))
        else:
            grouped.append(Paragraph(_to_rl_markup(text), body))
    flush_pending()

    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=title, author="Zoom Avatar Agent",
    ).build(grouped)
    return path


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #


def to_docx(md: str, path: pathlib.Path, title: str = "Meeting notes") -> pathlib.Path:
    import docx
    from docx.shared import Pt, RGBColor

    def _rgb(hex_colour: str) -> RGBColor:
        h = hex_colour.lstrip("#")
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    doc = docx.Document()
    doc.core_properties.title = title
    doc.core_properties.author = "Zoom Avatar Agent"

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    def add_runs(paragraph, text: str, *, size: float | None = None, colour: str | None = None) -> None:
        for chunk, bold, code in _inline_runs(text):
            if not chunk:
                continue
            run = paragraph.add_run(chunk)
            run.bold = bold
            if code:
                run.font.name = "Consolas"
            if size is not None:
                run.font.size = Pt(size)
            if colour is not None:
                run.font.color.rgb = _rgb(colour)

    for kind, text in _blocks(md):
        if kind == "h1":
            p = doc.add_paragraph()
            add_runs(p, text, size=20, colour=BRAND_BLUE)
            for r in p.runs:
                r.bold = True
        elif kind == "h2":
            p = doc.add_paragraph()
            add_runs(p, text, size=13, colour=BRAND_PURPLE)
            for r in p.runs:
                r.bold = True
        elif kind == "bullet":
            add_runs(doc.add_paragraph(style="List Bullet"), text)
        elif kind == "meta":
            add_runs(doc.add_paragraph(), text, size=10)
        elif kind == "quote":
            p = doc.add_paragraph(style="Intense Quote" if "Intense Quote" in [s.name for s in doc.styles] else None)
            add_runs(p, text, size=9, colour=MUTED)
        elif kind == "rule":
            doc.add_paragraph("_" * 60)
        else:
            add_runs(doc.add_paragraph(), text)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


FORMATS = {"pdf": to_pdf, "docx": to_docx}


def export(md: str, base_path: pathlib.Path, formats: list[str], title: str = "Meeting notes") -> list[pathlib.Path]:
    """Render `md` to each requested format alongside `base_path` (extension
    replaced). Unknown formats are skipped. Returns the paths written."""
    written: list[pathlib.Path] = []
    for fmt in formats:
        fn = FORMATS.get(fmt.strip().lower())
        if fn is None:
            continue
        written.append(fn(md, base_path.with_suffix(f".{fmt.strip().lower()}"), title=title))
    return written
