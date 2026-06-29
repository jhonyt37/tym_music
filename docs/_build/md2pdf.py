#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convierte los .md de TYM Music a PDF (reportlab, headless). Soporta imagenes."""
import os, re
import xml.sax.saxutils as sx
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                Image, PageBreak, ListFlowable, ListItem)
from reportlab.lib.utils import ImageReader

AZUL = colors.HexColor("#1F4E79")
AZUL2 = colors.HexColor("#2E75B6")
AZULBG = colors.HexColor("#EAF1F8")
GRIS = colors.HexColor("#555555")

ss = getSampleStyleSheet()
styles = {
    "Title": ParagraphStyle("T", parent=ss["Title"], textColor=AZUL, fontSize=26, spaceAfter=6),
    "Sub": ParagraphStyle("S", parent=ss["Normal"], textColor=AZUL2, fontSize=14,
                           alignment=TA_CENTER, spaceAfter=4),
    "SubIt": ParagraphStyle("SI", parent=ss["Normal"], textColor=GRIS, fontSize=9.5,
                            alignment=TA_CENTER, italic=True),
    "H1": ParagraphStyle("H1", parent=ss["Heading1"], textColor=AZUL, fontSize=15, spaceBefore=12, spaceAfter=6),
    "H2": ParagraphStyle("H2", parent=ss["Heading2"], textColor=AZUL2, fontSize=12.5, spaceBefore=8, spaceAfter=4),
    "H3": ParagraphStyle("H3", parent=ss["Heading3"], textColor=AZUL2, fontSize=11, spaceBefore=6, spaceAfter=3),
    "Body": ParagraphStyle("B", parent=ss["Normal"], fontSize=10, leading=14, spaceAfter=5, alignment=TA_LEFT),
    "Bullet": ParagraphStyle("Bu", parent=ss["Normal"], fontSize=10, leading=13, leftIndent=12, spaceAfter=2),
    "Quote": ParagraphStyle("Q", parent=ss["Normal"], fontSize=9.5, leading=13, textColor=GRIS,
                            italic=True, leftIndent=10, spaceAfter=5),
    "Cap": ParagraphStyle("Cap", parent=ss["Normal"], fontSize=8.5, textColor=GRIS,
                          italic=True, alignment=TA_CENTER, spaceAfter=8),
    "Cell": ParagraphStyle("Cell", parent=ss["Normal"], fontSize=8.8, leading=11),
    "CellB": ParagraphStyle("CellB", parent=ss["Normal"], fontSize=8.8, leading=11, fontName="Helvetica-Bold"),
    "CellH": ParagraphStyle("CellH", parent=ss["Normal"], fontSize=8.8, leading=11,
                            fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER),
}

REPL = {"→":"->","←":"<-","⇒":"=>","–":"-","—":"-","·":"-","•":"-","…":"...",
        "“":'"',"”":'"',"‘":"'","’":"'","‹":"<","›":">","≈":"~","±":"+/-",
        "✅":"[OK] ","❌":"[x] ","⚠️":"[!] ","⚠":"[!] ","⭐":"* ","🎯":"","📊":"",
        "💡":"","🔵":"","🔍":"","⚖️":"","💰":"","📋":"","📁":"","📈":"","😱":"","🤖":"","👋":""}

def sanitize(t):
    for k, v in REPL.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "ignore").decode("latin-1")

def inline(text):
    text = sanitize(text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("**") and p.endswith("**"):
            out.append("<b>" + sx.escape(p[2:-2]) + "</b>")
        elif p.startswith("`") and p.endswith("`"):
            out.append('<font face="Courier">' + sx.escape(p[1:-1]) + "</font>")
        else:
            out.append(sx.escape(p))
    return "".join(out)

def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]

def is_sep(line):
    return bool(re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", line)) and "-" in line

def make_table(header, rows, width):
    ncols = len(header)
    cw = [width / ncols] * ncols
    data = [[Paragraph(inline(h), styles["CellH"]) for h in header]]
    for row in rows:
        cells = []
        for i in range(ncols):
            val = row[i] if i < len(row) else ""
            st = styles["CellB"] if i == 0 else styles["Cell"]
            cells.append(Paragraph(inline(val), st))
        data.append(cells)
    t = Table(data, colWidths=cw, repeatRows=1)
    tstyle = [("BACKGROUND", (0, 0), (-1, 0), AZUL),
              ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C6D6")),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 3),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
              ("LEFTPADDING", (0, 0), (-1, -1), 4),
              ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    for ri in range(1, len(data)):
        if ri % 2 == 0:
            tstyle.append(("BACKGROUND", (0, ri), (-1, ri), AZULBG))
    t.setStyle(TableStyle(tstyle))
    return t

def image_flow(path, caption, width):
    try:
        iw, ih = ImageReader(path).getSize()
        w = min(width, 15 * cm)
        h = w * ih / iw
        flows = [Image(path, width=w, height=h)]
        if caption:
            flows.append(Paragraph(inline(caption), styles["Cap"]))
        return flows
    except Exception as e:
        return [Paragraph(f"[imagen no disponible: {os.path.basename(path)}]", styles["Body"])]

def parse_md(path, width):
    lines = open(path, encoding="utf-8").read().split("\n")
    flows = []
    pending_bullets = []

    def flush_bullets():
        nonlocal pending_bullets
        if pending_bullets:
            items = [ListItem(Paragraph(inline(b), styles["Bullet"]), leftIndent=10) for b in pending_bullets]
            flows.append(ListFlowable(items, bulletType="bullet", start="-", leftIndent=14))
            flows.append(Spacer(1, 4))
            pending_bullets = []

    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        s = raw.strip()
        if not s:
            flush_bullets(); i += 1; continue
        # Imagen ![cap](path)
        mi = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", s)
        if mi:
            flush_bullets()
            for fl in image_flow(mi.group(2), mi.group(1), width):
                flows.append(fl)
            i += 1; continue
        # Tabla
        if s.startswith("|") and i + 1 < len(lines) and is_sep(lines[i + 1]):
            flush_bullets()
            header = split_row(s); rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(split_row(lines[j])); j += 1
            flows.append(make_table(header, rows, width))
            flows.append(Spacer(1, 6))
            i = j; continue
        # Encabezado
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush_bullets()
            lvl = min(len(m.group(1)), 3)
            txt = re.sub(r"[`>]", "", m.group(2)).replace("*", "")
            flows.append(Paragraph(inline(txt), styles[f"H{lvl}"]))
            i += 1; continue
        # Blockquote
        if s.startswith(">"):
            flush_bullets()
            flows.append(Paragraph(inline(s.lstrip(">").strip()), styles["Quote"]))
            i += 1; continue
        # HR
        if s in ("---", "***", "___"):
            flush_bullets(); i += 1; continue
        # Code fence
        if s.startswith("```"):
            flush_bullets()
            j = i + 1; buf = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(sanitize(lines[j])); j += 1
            flows.append(Paragraph("<font face='Courier'>" +
                         sx.escape("\n".join(buf)).replace("\n", "<br/>") + "</font>", styles["Body"]))
            i = j + 1; continue
        # Bullets
        mb = re.match(r"^[-*]\s+(.*)$", s)
        if mb:
            pending_bullets.append(mb.group(1)); i += 1; continue
        mn = re.match(r"^\d+\.\s+(.*)$", s)
        if mn:
            pending_bullets.append(mn.group(1)); i += 1; continue
        # Parrafo
        flush_bullets()
        flows.append(Paragraph(inline(s), styles["Body"]))
        i += 1
    flush_bullets()
    return flows

def build(src, dest, title, subtitle):
    doc = SimpleDocTemplate(dest, pagesize=A4, topMargin=2*cm, bottomMargin=1.8*cm,
                            leftMargin=2*cm, rightMargin=2*cm, title=title, author="TYM Music")
    width = doc.width
    story = [Spacer(1, 5*cm),
             Paragraph("TYM MUSIC", ParagraphStyle("tt", parent=styles["Title"], alignment=TA_CENTER, fontSize=34)),
             Spacer(1, 0.3*cm),
             Paragraph(sanitize(title), styles["Sub"]),
             Spacer(1, 0.2*cm),
             Paragraph(sanitize(subtitle), styles["SubIt"]),
             PageBreak()]
    story += parse_md(src, width)

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GRIS)
        canvas.drawString(2*cm, 1*cm, "TYM Music - Documento de trabajo")
        canvas.drawRightString(A4[0]-2*cm, 1*cm, f"Pag. {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print("OK ->", dest)

DOCS = "/Users/jhonytoro/Documents/emprendimiento/tym_music/docs"
SCRATCH = os.path.dirname(__file__)
jobs = [
    (f"{DOCS}/00-MASTER-BRIEF.md", f"{DOCS}/TYM_Music_00_Master_Brief.pdf",
     "Master Brief - Resumen Ejecutivo de Decisiones", "Documento maestro del proyecto - Junio 2026"),
    ("/Users/jhonytoro/.claude/plans/immutable-enchanting-lemur.md",
     f"{DOCS}/TYM_Music_01_Estudio_de_Mercado.pdf",
     "Estudio de Mercado y Viabilidad",
     "Benchmark - Competidores - Oceano rojo/azul - Warnings - Colombia"),
    (f"{SCRATCH}/flagship.md", f"{DOCS}/TYM_Music_02_Analisis_Completo_con_Graficas.pdf",
     "Analisis Completo con Graficas",
     "Mercado - Modelo - Costos - Unit economics - Proyecciones - Junio 2026"),
    (f"{DOCS}/02-modelo-negocio-y-costos.md", f"{DOCS}/TYM_Music_03_Modelo_y_Costos.pdf",
     "Modelo de Negocio y Analisis de Costos",
     "Pay-per-priority - Unit economics - Costos operativos - Junio 2026"),
]
for src, dest, title, sub in jobs:
    if os.path.exists(src):
        build(src, dest, title, sub)
    else:
        print("FALTA:", src)
print("Listo.")
