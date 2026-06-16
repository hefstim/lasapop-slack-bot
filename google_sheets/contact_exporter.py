"""Export customers with missing contact info (phone/email/Facebook/Viber) to
Google Sheets for employees to fill in by hand. No Odoo data is touched here —
this is a read-only export; syncing the filled-in sheet back to Odoo is a
separate, explicit step."""
import logging
from config import Config
from google_sheets.exporter import (
    _get_client, _get_or_create_ws,
    _freeze_row, _header_style, _currency_format, _auto_resize,
    _fetch_delete_protections, _protect_cols,
)

logger = logging.getLogger(__name__)

CONTACT_SHEET = "Customer Contact Info"
CONTACT_HEADERS = [
    "#", "Customer Name", "Phone", "Email", "Facebook", "Viber",
    "Order Count (2yr)", "Total Amount (2yr)", "Partner ID",
]
HEADER_COLOR = {"red": 0.20, "green": 0.45, "blue": 0.55}


def export_missing_contacts(customers: list[dict]) -> str:
    """customers: list of dicts with partner_id, name, phone, email, facebook,
    viber, order_count, total_amount — already sorted by caller."""
    gc = _get_client()
    sh = gc.open_by_key(Config.GOOGLE_SHEET_ID)
    ws = _get_or_create_ws(sh, CONTACT_SHEET, len(CONTACT_HEADERS))

    data = [CONTACT_HEADERS]
    for i, c in enumerate(customers, 1):
        data.append([
            i, c["name"], c.get("phone") or "", c.get("email") or "",
            c.get("facebook") or "", c.get("viber") or "",
            c["order_count"], c["total_amount"], c["partner_id"],
        ])

    ws.clear()
    if len(data) > 1:
        ws.update(data, value_input_option="USER_ENTERED")

    sheet_id = ws._properties["sheetId"]
    n = len(customers)

    reqs = [
        _freeze_row(sheet_id),
        _header_style(sheet_id, len(CONTACT_HEADERS), HEADER_COLOR),
        _currency_format(sheet_id, col_idx=7, start_row=1, end_row=n + 1),  # col H = Total Amount
        _auto_resize(sheet_id, len(CONTACT_HEADERS)),
    ]
    sh.batch_update({"requests": reqs})

    # CONTACT_HEADERS layout (0-indexed):
    # 0=#  1=Name  2=Phone  3=Email  4=Facebook  5=Viber
    # 6=OrderCount  7=TotalAmount  8=PartnerID
    # Lock #, Name, Order Count, Total Amount, Partner ID — leave Phone/Email/
    # Facebook/Viber (2-5) editable for staff to fill in.
    del_reqs = _fetch_delete_protections(sh, sheet_id)
    add_reqs = _protect_cols(sheet_id,
                             col_ranges=[(0, 2), (6, 9)],
                             num_rows=n,
                             description="Auto-filled by bot — do not edit")
    if del_reqs or add_reqs:
        sh.batch_update({"requests": del_reqs + add_reqs})

    return f"https://docs.google.com/spreadsheets/d/{Config.GOOGLE_SHEET_ID}/edit#gid={sheet_id}"
