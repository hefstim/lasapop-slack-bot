"""Export overdue invoices to Google Sheets for human follow-up / verification.
No Odoo data is touched here — these are read-only exports; humans update the
Status column in the sheet to track their own progress."""
import logging
from config import Config
from google_sheets.exporter import (
    _get_client, _get_or_create_ws,
    _freeze_row, _header_style, _currency_format, _dropdown, _row_color, _auto_resize,
    _fetch_delete_protections, _protect_cols,
)

logger = logging.getLogger(__name__)

INVOICE_HEADERS = [
    "#", "Invoice No", "Customer", "Sales Rep",
    "Amount Due", "Due Date", "Days Overdue",
    "Status", "Notes", "Last Updated By", "Invoice ID",
]

# ── Active follow-up sheet (within the daily reminder window, ≤30 days) ───────

OVERDUE_SHEET = "Overdue Invoices"
OVERDUE_STATUS_OPTIONS = ["Pending", "Contacted", "Payment Promised", "Paid", "Disputed"]
OVERDUE_HEADER_COLOR = {"red": 0.65, "green": 0.40, "blue": 0.05}
OVERDUE_STATUS_COLORS = {
    "Pending": {"red": 1.0, "green": 0.98, "blue": 0.86},
    "Contacted": {"red": 1.0, "green": 0.93, "blue": 0.80},
    "Payment Promised": {"red": 0.80, "green": 0.88, "blue": 1.0},
    "Paid": {"red": 0.82, "green": 0.96, "blue": 0.84},
    "Disputed": {"red": 1.0, "green": 0.80, "blue": 0.80},
}

# ── Legacy review sheet (>30 days, manual payment verification) ──────────────

LEGACY_SHEET = "Legacy Overdue Invoices"
LEGACY_STATUS_OPTIONS = ["Pending Review", "Confirmed Paid", "Confirmed Unpaid - Follow Up", "Write Off"]
LEGACY_HEADER_COLOR = {"red": 0.55, "green": 0.15, "blue": 0.15}
LEGACY_STATUS_COLORS = {
    "Pending Review": {"red": 1.0, "green": 0.98, "blue": 0.86},
    "Confirmed Paid": {"red": 0.82, "green": 0.96, "blue": 0.84},
    "Confirmed Unpaid - Follow Up": {"red": 1.0, "green": 0.85, "blue": 0.78},
    "Write Off": {"red": 0.85, "green": 0.85, "blue": 0.85},
}


def _export_invoice_sheet(sheet_name: str, status_options: list, status_colors: dict,
                          header_color: dict, default_status: str,
                          invoices: list[dict]) -> str:
    """Write an invoice review/follow-up sheet, preserving any Status/Notes/Handler
    already entered by a human when the same invoice is exported again."""
    gc = _get_client()
    sh = gc.open_by_key(Config.GOOGLE_SHEET_ID)
    ws = _get_or_create_ws(sh, sheet_name, len(INVOICE_HEADERS))

    existing: dict[int, dict] = {}
    try:
        for row in ws.get_all_records(head=1):
            inv_id = row.get("Invoice ID")
            if inv_id:
                existing[int(inv_id)] = {
                    "status": str(row.get("Status", default_status)) or default_status,
                    "notes": str(row.get("Notes", "")),
                    "handler": str(row.get("Last Updated By", "")),
                }
    except Exception:
        pass

    data = [INVOICE_HEADERS]
    row_colors = []
    for i, inv in enumerate(invoices, 1):
        prev = existing.get(inv["invoice_id"], {})
        status = prev.get("status", default_status)
        data.append([
            i, inv["invoice_name"], inv["partner_name"], inv["salesperson"],
            inv["amount_residual"], inv["due_date"], inv["days_overdue"],
            status,
            prev.get("notes", ""),
            prev.get("handler", ""),
            inv["invoice_id"],
        ])
        row_colors.append(status_colors.get(status, status_colors[default_status]))

    ws.clear()
    if len(data) > 1:
        ws.update(data, value_input_option="USER_ENTERED")

    sheet_id = ws._properties["sheetId"]
    n = len(invoices)

    # INVOICE_HEADERS layout (0-indexed):
    # 0=#  1=InvoiceNo  2=Customer  3=SalesRep  4=AmountDue  5=DueDate  6=DaysOverdue
    # 7=Status  8=Notes  9=LastUpdatedBy  10=InvoiceID
    reqs = [
        _freeze_row(sheet_id),
        _header_style(sheet_id, len(INVOICE_HEADERS), header_color),
        _currency_format(sheet_id, col_idx=4, start_row=1, end_row=n + 1),
        _dropdown(sheet_id, col_idx=7, start_row=1, end_row=n + 1, options=status_options),
        _auto_resize(sheet_id, len(INVOICE_HEADERS)),
    ]
    for idx, color in enumerate(row_colors):
        reqs.append(_row_color(sheet_id, idx + 1, color, len(INVOICE_HEADERS)))
    sh.batch_update({"requests": reqs})

    # Lock auto-filled columns (0-6) and Invoice ID (10); leave Status/Notes/Handler (7,8,9) editable
    del_reqs = _fetch_delete_protections(sh, sheet_id)
    add_reqs = _protect_cols(sheet_id,
                             col_ranges=[(0, 7), (10, 11)],
                             num_rows=n,
                             description="Auto-filled by bot — do not edit")
    if del_reqs or add_reqs:
        sh.batch_update({"requests": del_reqs + add_reqs})

    return f"https://docs.google.com/spreadsheets/d/{Config.GOOGLE_SHEET_ID}/edit#gid={sheet_id}"


def export_overdue_invoices(invoices: list[dict]) -> str:
    """Invoices within the daily reminder window (≤30 days) — for sales/finance
    to actively follow up and collect payment."""
    return _export_invoice_sheet(
        OVERDUE_SHEET, OVERDUE_STATUS_OPTIONS, OVERDUE_STATUS_COLORS,
        OVERDUE_HEADER_COLOR, "Pending", invoices,
    )


def export_legacy_invoices(invoices: list[dict]) -> str:
    """Invoices beyond the daily reminder window (>30 days) — for manual
    one-by-one payment verification (likely data entry errors, not bad debt)."""
    return _export_invoice_sheet(
        LEGACY_SHEET, LEGACY_STATUS_OPTIONS, LEGACY_STATUS_COLORS,
        LEGACY_HEADER_COLOR, "Pending Review", invoices,
    )
