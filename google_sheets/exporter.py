"""
Two-sheet Google Sheets exporter for dormant customers.

Sheet 1 "Dormant Customers"  — Pending customers only (needs follow-up)
Sheet 2 "Follow-up Tracker"  — Contacted customers with cooldown dates
"""
import logging
import requests as _requests
from datetime import datetime, date, timedelta
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as _GoogleRequest
from config import Config
import db

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MAIN_SHEET    = "Dormant Customers"
TRACKER_SHEET = "Follow-up Tracker"

MAIN_HEADERS = [
    "#", "Customer Name", "Phone", "Viber", "Facebook",
    "Email", "Total Orders", "Total Purchase History",
    "Last Purchase", "Days Inactive", "Sales Rep",
    "Status", "Notes", "Last Updated By", "Partner ID",
]

TRACKER_HEADERS = [
    "#", "Customer Name", "Phone", "Viber", "Facebook",
    "Email", "Total Orders", "Total Purchase History",
    "Last Purchase", "Sales Rep",
    "Contacted Date", "Re-contact Date", "Days Until Re-contact",
    "Notes", "Partner ID",
]

STATUS_OPTIONS   = ["Pending", "Contacted", "Won't Buy", "Reactivated"]
HEADER_COLOR     = {"red": 0.27, "green": 0.29, "blue": 0.65}
TRACKER_HDR_COLOR = {"red": 0.18, "green": 0.49, "blue": 0.42}

# Module-level credentials cache (needed for protection API calls)
_creds: Credentials | None = None


# ── Client ────────────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    global _creds
    _creds = Credentials.from_service_account_file(
        Config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return gspread.authorize(_creds)


def _get_or_create_ws(sh: gspread.Spreadsheet, name: str, cols: int = 15) -> gspread.Worksheet:
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=2000, cols=cols)


# ── Main export entry point ───────────────────────────────────────────────────

def export_dormant_customers(customers: list[dict]) -> str:
    """
    1. Read tracker sheet  → who's in cooldown, who has expired cooldown
    2. Read main sheet     → detect newly-Contacted rows (employee updated)
    3. Write main sheet    → only Pending customers (exclude cooldown & excluded)
    4. Write tracker sheet → in-cooldown + newly-contacted; remove expired ones
    Returns the main sheet URL.
    """
    gc  = _get_client()
    sh  = gc.open_by_key(Config.GOOGLE_SHEET_ID)
    ws_main    = _get_or_create_ws(sh, MAIN_SHEET,    len(MAIN_HEADERS))
    ws_tracker = _get_or_create_ws(sh, TRACKER_SHEET, len(TRACKER_HEADERS))

    today         = date.today()
    cooldown_days = Config.CONTACTED_COOLDOWN_DAYS

    # ── Step 1: read tracker ──────────────────────────────────────────────────
    in_cooldown: dict[int, dict] = {}      # partner_id → tracker row data
    expired_pids: set[int] = set()

    try:
        t_rows = ws_tracker.get_all_records(head=1)
    except Exception:
        t_rows = []

    for row in t_rows:
        pid = row.get("Partner ID")
        if not pid:
            continue
        pid = int(pid)
        recontact_str = str(row.get("Re-contact Date", "")).strip()
        if not recontact_str:
            continue
        try:
            recontact = datetime.strptime(recontact_str, "%Y-%m-%d").date()
            if recontact > today:
                in_cooldown[pid] = {
                    "contacted_date":  str(row.get("Contacted Date", "")),
                    "recontact_date":  recontact_str,
                    "notes":           str(row.get("Notes", "")),
                }
            else:
                expired_pids.add(pid)
                logger.info("Cooldown expired for partner_id=%d, moving back to main sheet.", pid)
        except ValueError:
            pass

    # ── Step 2: read main sheet — detect newly Contacted ─────────────────────
    newly_contacted: dict[int, dict] = {}   # partner_id → {notes, handler}
    existing_notes:  dict[int, dict] = {}   # preserve notes for Pending rows

    try:
        m_rows = ws_main.get_all_records(head=1)
    except Exception:
        m_rows = []

    for row in m_rows:
        pid = row.get("Partner ID")
        if not pid:
            continue
        pid = int(pid)
        status  = str(row.get("Status", "Pending")).strip()
        notes   = str(row.get("Notes", "")).strip()
        handler = str(row.get("Last Updated By", "")).strip()

        if status == "Contacted":
            newly_contacted[pid] = {"notes": notes, "handler": handler}
        elif status == "Pending":
            existing_notes[pid]  = {"notes": notes, "handler": handler}

    # ── Step 3: build & write main sheet (Pending only) ──────────────────────
    excluded_ids = db.get_excluded_partner_ids()

    # Customers that belong in main sheet:
    # - all dormant customers
    # - MINUS those in active cooldown (still in tracker)
    # - MINUS those just marked Contacted (going to tracker now)
    # - MINUS permanently excluded (Won't Buy)
    # + expired cooldown customers return here as Pending
    pending_customers = [
        c for c in customers
        if c["partner_id"] not in in_cooldown
        and c["partner_id"] not in newly_contacted
        and c["partner_id"] not in excluded_ids
    ]

    _write_main_sheet(ws_main, sh, pending_customers, existing_notes)
    logger.info("Main sheet: %d pending customers.", len(pending_customers))

    # ── Step 4: build & write tracker sheet ──────────────────────────────────
    # customer lookup for enriching tracker rows
    customer_map = {c["partner_id"]: c for c in customers}

    tracker_rows: list[dict] = []

    # Keep existing cooldown entries (not expired, not newly reactivated/excluded)
    for pid, cd in in_cooldown.items():
        if pid in expired_pids or pid in excluded_ids:
            continue
        c = customer_map.get(pid)
        if c:
            tracker_rows.append({**c, **cd})

    # Add newly contacted customers
    for pid, info in newly_contacted.items():
        c = customer_map.get(pid)
        if not c:
            continue
        contact_date   = today.isoformat()
        recontact_date = (today + timedelta(days=cooldown_days)).isoformat()
        tracker_rows.append({
            **c,
            "contacted_date": contact_date,
            "recontact_date": recontact_date,
            "notes":          info["notes"],
        })
        logger.info("Moved to tracker: %s (re-contact on %s)", c["name"], recontact_date)

    # Sort tracker by re-contact date ascending
    tracker_rows.sort(key=lambda x: x.get("recontact_date", ""))

    _write_tracker_sheet(ws_tracker, sh, tracker_rows, today)
    logger.info("Tracker sheet: %d customers in cooldown.", len(tracker_rows))

    sheet_id = ws_main._properties["sheetId"]
    return f"https://docs.google.com/spreadsheets/d/{Config.GOOGLE_SHEET_ID}/edit#gid={sheet_id}"


# ── Sheet writers ─────────────────────────────────────────────────────────────

def _write_main_sheet(ws: gspread.Worksheet, sh: gspread.Spreadsheet,
                      customers: list[dict], existing_notes: dict):
    data = [MAIN_HEADERS]
    for i, c in enumerate(customers, 1):
        prev = existing_notes.get(c["partner_id"], {})
        data.append([
            i, c["name"], c["phone"], c.get("viber", ""), c.get("facebook", ""),
            c["email"], c["order_count"], c["last_order_amount"],
            c["last_order_date"], c["days_inactive"], c["salesperson"],
            "Pending",
            prev.get("notes", ""),
            prev.get("handler", ""),
            c["partner_id"],
        ])

    ws.clear()
    if len(data) > 1:
        ws.update(data, value_input_option="USER_ENTERED")

    sheet_id = ws._properties["sheetId"]
    n = len(customers)

    # MAIN_HEADERS layout (0-indexed):
    # 0=#  1=Name  2=Phone  3=Viber  4=Facebook  5=Email
    # 6=Orders  7=History  8=LastPurchase  9=DaysInactive  10=SalesRep
    # 11=Status  12=Notes  13=LastUpdatedBy  14=PartnerID
    reqs = [
        _freeze_row(sheet_id),
        _header_style(sheet_id, len(MAIN_HEADERS), HEADER_COLOR),
        _currency_format(sheet_id, col_idx=7, start_row=1, end_row=n + 1),  # col H = Total Purchase History
        _dropdown(sheet_id, col_idx=11, start_row=1, end_row=n + 1, options=STATUS_OPTIONS),
        _auto_resize(sheet_id, len(MAIN_HEADERS)),
    ]
    for row_idx in range(1, n + 1):
        reqs.append(_row_color(sheet_id, row_idx,
                               {"red": 1.0, "green": 0.98, "blue": 0.86},
                               len(MAIN_HEADERS)))
    sh.batch_update({"requests": reqs})

    # Lock A-K (auto data) and O (Partner ID); leave L/M/N (Status/Notes/LastUpdatedBy) editable
    # A-K = cols 0-11, O = cols 14-15
    del_reqs = _fetch_delete_protections(sh, sheet_id)
    add_reqs = _protect_cols(sheet_id,
                             col_ranges=[(0, 11), (14, 15)],
                             num_rows=n,
                             description="Auto-filled by bot — do not edit")
    if del_reqs or add_reqs:
        sh.batch_update({"requests": del_reqs + add_reqs})


def _write_tracker_sheet(ws: gspread.Worksheet, sh: gspread.Spreadsheet,
                         customers: list[dict], today: date):
    data = [TRACKER_HEADERS]
    for i, c in enumerate(customers, 1):
        recontact = c.get("recontact_date", "")
        days_left = ""
        if recontact:
            try:
                days_left = (datetime.strptime(recontact, "%Y-%m-%d").date() - today).days
            except ValueError:
                pass
        data.append([
            i, c["name"], c["phone"], c.get("viber", ""), c.get("facebook", ""),
            c["email"], c["order_count"], c["last_order_amount"],
            c["last_order_date"], c["salesperson"],
            c.get("contacted_date", ""),
            recontact,
            days_left,
            c.get("notes", ""),
            c["partner_id"],
        ])

    ws.clear()
    if len(data) > 1:
        ws.update(data, value_input_option="USER_ENTERED")

    sheet_id = ws._properties["sheetId"]
    n = len(customers)

    # Formatting requests
    reqs = [
        _freeze_row(sheet_id),
        _header_style(sheet_id, len(TRACKER_HEADERS), TRACKER_HDR_COLOR),
        _currency_format(sheet_id, col_idx=7, start_row=1, end_row=n + 1),  # col H = Total Purchase History
        _auto_resize(sheet_id, len(TRACKER_HEADERS)),
    ]
    for idx, c in enumerate(customers):
        recontact = c.get("recontact_date", "")
        color = {"red": 0.82, "green": 0.96, "blue": 0.84}  # green
        if recontact:
            try:
                dl = (datetime.strptime(recontact, "%Y-%m-%d").date() - today).days
                if dl <= 7:
                    color = {"red": 1.0, "green": 0.93, "blue": 0.80}  # orange
            except ValueError:
                pass
        reqs.append(_row_color(sheet_id, idx + 1, color, len(TRACKER_HEADERS)))
    sh.batch_update({"requests": reqs})

    # TRACKER_HEADERS layout (0-indexed):
    # 0=#  1=Name  2=Phone  3=Viber  4=Facebook  5=Email
    # 6=Orders  7=History  8=LastPurchase  9=SalesRep
    # 10=ContactedDate  11=RecontactDate  12=DaysUntilRecontact
    # 13=Notes  14=PartnerID
    # Lock everything except Notes (col 13); leave col 13 editable
    del_reqs = _fetch_delete_protections(sh, sheet_id)
    add_reqs = _protect_cols(sheet_id,
                             col_ranges=[(0, 13), (14, 15)],
                             num_rows=n,
                             description="Auto-managed by bot — do not edit")
    if del_reqs or add_reqs:
        sh.batch_update({"requests": del_reqs + add_reqs})


# ── Column protection helpers ─────────────────────────────────────────────────

def _fetch_delete_protections(sh: gspread.Spreadsheet, sheet_id: int) -> list:
    """Return deleteProtectedRange requests for all existing protections on a sheet."""
    global _creds
    if _creds and not _creds.valid:
        _creds.refresh(_GoogleRequest())
    token = _creds.token if _creds else ""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sh.id}"
    resp = _requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "sheets(properties/sheetId,protectedRanges/protectedRangeId)"},
        timeout=15,
    )
    reqs = []
    for s in resp.json().get("sheets", []):
        if s.get("properties", {}).get("sheetId") != sheet_id:
            continue
        for pr in s.get("protectedRanges", []):
            reqs.append({"deleteProtectedRange": {"protectedRangeId": pr["protectedRangeId"]}})
    return reqs


def _protect_cols(sheet_id: int, col_ranges: list[tuple[int, int]],
                  num_rows: int, description: str) -> list:
    """Build addProtectedRange requests. col_ranges: list of (start_col_idx, end_col_idx exclusive)."""
    # Must include the service account as an editor, otherwise the API rejects the request
    service_email = _creds.service_account_email if _creds else ""
    editors = {"users": [service_email], "groups": [], "domainUsersCanEdit": False} if service_email else {}
    reqs = []
    for start_col, end_col in col_ranges:
        req = {"addProtectedRange": {"protectedRange": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": num_rows + 1,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "description": description,
            "warningOnly": False,
        }}}
        if editors:
            req["addProtectedRange"]["protectedRange"]["editors"] = editors
        reqs.append(req)
    return reqs


# ── Formatting helpers ────────────────────────────────────────────────────────

def _freeze_row(sheet_id: int) -> dict:
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }}


def _header_style(sheet_id: int, num_cols: int, bg_color: dict) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": num_cols},
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": bg_color,
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)",
    }}


def _currency_format(sheet_id: int, col_idx: int, start_row: int, end_row: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": start_row, "endRowIndex": end_row,
                  "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "₱#,##0.00"}}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def _dropdown(sheet_id: int, col_idx: int, start_row: int, end_row: int, options: list) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": start_row, "endRowIndex": end_row,
                  "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
        "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": o} for o in options]},
            "showCustomUi": True, "strict": True,
        },
    }}


def _row_color(sheet_id: int, row_idx: int, color: dict, num_cols: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id,
                  "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                  "startColumnIndex": 0, "endColumnIndex": num_cols},
        "cell": {"userEnteredFormat": {"backgroundColor": color}},
        "fields": "userEnteredFormat.backgroundColor",
    }}


def _auto_resize(sheet_id: int, num_cols: int) -> dict:
    return {"autoResizeDimensions": {"dimensions": {
        "sheetId": sheet_id, "dimension": "COLUMNS",
        "startIndex": 0, "endIndex": num_cols,
    }}}


# ── Sync Won't Buy ────────────────────────────────────────────────────────────

def sync_wont_buy_to_blacklist() -> int:
    gc = _get_client()
    sh = gc.open_by_key(Config.GOOGLE_SHEET_ID)

    added = 0
    already_excluded = db.get_excluded_partner_ids()

    for sheet_name in [MAIN_SHEET, TRACKER_SHEET]:
        try:
            ws   = sh.worksheet(sheet_name)
            rows = ws.get_all_records(head=1)
        except Exception:
            continue

        for row in rows:
            if str(row.get("Status", "")).strip() != "Won't Buy":
                continue
            pid = row.get("Partner ID")
            if not pid:
                continue
            pid = int(pid)
            if pid in already_excluded:
                continue
            name    = str(row.get("Customer Name", ""))
            handler = str(row.get("Last Updated By", "Google Sheet"))
            db.exclude_customer(pid, name, handler or "Google Sheet")
            already_excluded.add(pid)
            logger.info("Blacklisted from sheet: %s (id=%d)", name, pid)
            added += 1

    return added


def get_sheet_url() -> str:
    gc = _get_client()
    sh = gc.open_by_key(Config.GOOGLE_SHEET_ID)
    try:
        ws       = sh.worksheet(MAIN_SHEET)
        sheet_id = ws._properties["sheetId"]
    except gspread.WorksheetNotFound:
        sheet_id = 0
    return f"https://docs.google.com/spreadsheets/d/{Config.GOOGLE_SHEET_ID}/edit#gid={sheet_id}"
