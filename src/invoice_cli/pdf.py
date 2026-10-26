"""Direct, atomic PDF exports; no browser required."""
from __future__ import annotations

from html import escape
import os
from pathlib import Path
import tempfile

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .core import money, price_display
from .storage import Error, private_dir


def destination(inv, output=None):
    basename = f"{inv['id']:06d}-{inv['customer']['slug']}.pdf"
    if output is None:
        return Path.cwd() / basename
    raw = str(output)
    path = Path(raw).expanduser()
    if path.is_dir() or raw.endswith(("/", os.sep)):
        return path / basename
    if path.suffix.lower() != ".pdf":
        raise Error("Output must be a .pdf file or a directory (end new directories with /)")
    return path


def render(inv, output=None, overwrite=False):
    path = destination(inv, output).absolute()
    if path.exists() and not overwrite:
        raise Error(f"Output already exists: {path}. Use --overwrite explicitly.")
    private_dir(path.parent)
    fonts = Path(__file__).parent / "fonts"
    if "InvoiceSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("InvoiceSans", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("InvoiceSansBold", str(fonts / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("InvoiceSans", normal="InvoiceSans", bold="InvoiceSansBold", italic="InvoiceSans", boldItalic="InvoiceSansBold")
    styles = getSampleStyleSheet()
    body = ParagraphStyle("InvoiceBody", fontName="InvoiceSans", fontSize=9, leading=13, spaceAfter=4)
    title = ParagraphStyle("InvoiceTitle", parent=body, fontName="InvoiceSansBold", fontSize=24, leading=30, spaceAfter=10)
    small = ParagraphStyle("InvoiceSmall", parent=body, fontSize=8, leading=11)
    right = ParagraphStyle("InvoiceRight", parent=small, alignment=TA_RIGHT, splitLongWords=True)

    def para(text, style=body):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    status = "" if inv["status"] == "posted" else f" - {inv['status'].upper()}"
    story = [para(f"INVOICE {inv['id']:06d}{status}", title)]
    parties = []
    for label, key in [("FROM", "business"), ("BILL TO", "customer")]:
        party = inv[key]
        lines = [label, party["name"], party["address"]]
        if party.get("email"):
            lines.append(party["email"])
        if party.get("gst_number"):
            lines.append("GST registration: " + party["gst_number"])
        parties.append(para("\n".join(lines)))
    table = Table([parties], colWidths=[3.25 * inch, 3.25 * inch])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    metadata = Table([[para(f"Issued: {inv['issue_date']}"), para(f"Due: {inv['due_date']}"), para(f"Currency: {inv['currency']}")]],
                     colWidths=[2.4 * inch, 2.4 * inch, 1.7 * inch])
    metadata.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [table, Spacer(1, 12), metadata, Spacer(1, 12)]
    rows = [[para(x, small) for x in ["Description", "Quantity", "Unit price", "Tax", "Amount"]]]
    for item in inv["lines"]:
        tax = item["tax"]
        if tax not in {"exempt", "zero-rated", "none"}:
            tax += f" {item['tax_rate']}%"
        rows.append([para(item["description"], small), para(f"{item['quantity']} {item['unit']}", right),
                     para(price_display(item["unit_price"]), right), para(tax, small), para(money(item["net_cents"]), right)])
    table = Table(rows, colWidths=[2.5 * inch, .9 * inch, 1.05 * inch, .9 * inch, 1.15 * inch], repeatRows=1,
                  hAlign="LEFT", splitByRow=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), .6, colors.HexColor("#65758A")),
        ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#DCE2E8")),
    ]))
    story += [table, Spacer(1, 14)]
    t = inv["totals"]
    sums = [[para("Subtotal"), para(money(t["subtotal_cents"]), right)]]
    sums += [[para(f"{x['label']} ({x['rate']}%)"), para(money(x["cents"]), right)] for x in t["taxes"]]
    sums.append([para("TOTAL " + inv["currency"], ParagraphStyle("TotalLabel", parent=body, fontName="InvoiceSansBold")),
                 para(money(t["total_cents"]), ParagraphStyle("TotalValue", parent=right, fontName="InvoiceSansBold", fontSize=11))])
    summary = Table(sums, colWidths=[2.5 * inch, 1.35 * inch], hAlign="RIGHT")
    summary.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEABOVE", (0, -1), (-1, -1), .8, colors.HexColor("#65758A")),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.append(KeepTogether([summary]))
    if inv["note"]:
        story += [Spacer(1, 18), para("Note", ParagraphStyle("NoteTitle", parent=body, fontName="InvoiceSansBold")), para(inv["note"])]
    if inv.get("void_reason"):
        story += [Spacer(1, 12), para("VOID: " + inv["void_reason"])]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("InvoiceSans", 8)
        canvas.setFillColor(colors.HexColor("#65758A"))
        canvas.drawString(inch, .5 * inch, f"Invoice {inv['id']:06d}{status}")
        canvas.drawRightString(7.5 * inch, .5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    fd, temporary = tempfile.mkstemp(prefix=".invoice-", suffix=".pdf", dir=path.parent)
    os.close(fd)
    try:
        doc = SimpleDocTemplate(temporary, pagesize=(8.5 * inch, 11 * inch), leftMargin=inch, rightMargin=inch,
                                topMargin=.65 * inch, bottomMargin=.8 * inch, title=f"Invoice {inv['id']:06d}", author=inv["business"]["name"])
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber publication, even if another process creates it.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise Error(f"Output already exists: {path}. Use --overwrite explicitly.") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path
