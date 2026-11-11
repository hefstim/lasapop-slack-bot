"""Printable production order / material list for a Slack production plan (A4 PDF).

Odoo's own report PDFs cannot be rendered through the JSON-2 API, so the bot draws
its own: lot and expiry for the inkjet printer, materials with where to take them,
blanks to write the real figures, signatures.
"""
import io
from datetime import datetime

from reportlab.graphics.barcode import code128
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONT = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
pdfmetrics.registerFont(TTFont("CJK", FONT, subfontIndex=0))

_base = ParagraphStyle("b", fontName="CJK", fontSize=9.5, leading=13)
_small = ParagraphStyle("s", parent=_base, fontSize=8, leading=11, textColor=colors.HexColor("#555555"))
_title = ParagraphStyle("t", parent=_base, fontSize=17, leading=22)
_big = ParagraphStyle("g", parent=_base, fontSize=15, leading=20)
GRID = colors.HexColor("#999999")
HEAD = colors.HexColor("#e8eef5")


def _p(text, style=_base):
    return Paragraph(text, style)


def production_order_pdf(mo: dict, materials: list[dict], planner: str, note: str = "") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=f"{mo['name']} 生产单")
    w = A4[0] - 28 * mm
    story = []
    barcode = code128.Code128(mo["name"], barHeight=11 * mm, barWidth=0.33 * mm)
    head = Table([[_p(f"生产单 / Production Order<br/><font size=12>Lasa Food Corporation</font>", _title), barcode]],
                 colWidths=[w - 62 * mm, 62 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [head, Spacer(1, 4 * mm)]

    info = [
        [_p("生产单号 / MO"), _p(f"<b>{mo['name']}</b>"), _p("计划日期 / Date"), _p(mo["day"] or "")],
        [_p("产品 / Product"), _p(mo["product_id"][1]), _p("计划数量 / Qty"), _p(f"{mo['product_qty']:g} {mo['product_uom_id'][1]}")],
        [_p("成品批号 / Lot"), _p(f"<b>{mo['lot']}</b>", _big), _p("到期日 / Expiry"), _p(f"<b>{mo['expiry']}</b>", _big)],
        [_p("计划人 / Planned by"), _p(planner), _p("打印时间 / Printed"), _p(datetime.now().strftime("%Y-%m-%d %H:%M"))],
    ]
    if note:
        info.append([_p("备注 / Note"), _p(note), "", ""])
    t = Table(info, colWidths=[30 * mm, w / 2 - 30 * mm, 30 * mm, w / 2 - 30 * mm])
    style = [("GRID", (0, 0), (-1, -1), 0.5, GRID), ("BACKGROUND", (0, 0), (0, -1), HEAD),
             ("BACKGROUND", (2, 0), (2, -1), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    if note:
        style.append(("SPAN", (1, len(info) - 1), (3, len(info) - 1)))
    t.setStyle(TableStyle(style))
    story += [t, Spacer(1, 2 * mm),
              _p("喷码机设成上面的批号和到期日，第一袋喷出来先核对。 Set the inkjet printer to this lot and expiry; check the first pack.", _small),
              Spacer(1, 5 * mm), _p("<b>备料清单 / Materials</b>（先从配料间拿，不够再从主原料区；先到期的先用）"), Spacer(1, 2 * mm)]

    rows = [[_p(h) for h in ("原料 / Material", "计划用量<br/>Planned", "配料间可用<br/>Batching", "主原料区可用<br/>Main Store",
                             "先用批次 / Use first", "实际用量<br/>Actual", "备料人<br/>By")]]
    for m in materials:
        need = f"{m['need']:g} {m['uom']}" + (f"<br/><font color='#c0392b'>缺 {m['short']:g}</font>" if m["short"] > 1e-6 else "")
        rows.append([_p(m["product"]), _p(need), _p(f"{m['free'].get('Batching Room', 0):g}"),
                     _p(f"{m['free'].get('Main Store', 0):g}"), _p("<br/>".join(m["lots"]) or "—", _small), "", ""])
    mt = Table(rows, colWidths=[46 * mm, 22 * mm, 20 * mm, 22 * mm, 34 * mm, 20 * mm, w - 164 * mm], repeatRows=1)
    mt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 1), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 1), (-1, -1), 6)]))
    story += [mt, Spacer(1, 7 * mm), _p("<b>完工记录 / Result</b>（完工后在 Slack「完工登记」填同样的数字）"), Spacer(1, 2 * mm)]

    res = Table([[_p("实际产量 / Output"), "", _p("不良品 / Rejects"), ""],
                 [_p("开始时间 / Start"), "", _p("结束时间 / End"), ""],
                 [_p("生产主管 / Supervisor"), "", _p("配料员 / Batching"), ""]],
                colWidths=[34 * mm, w / 2 - 34 * mm, 34 * mm, w / 2 - 34 * mm], rowHeights=[11 * mm] * 3)
    res.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, GRID), ("BACKGROUND", (0, 0), (0, -1), HEAD),
                             ("BACKGROUND", (2, 0), (2, -1), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [res]
    doc.build(story)
    return buf.getvalue()
