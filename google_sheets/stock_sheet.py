"""读取库存盘点 Sheet 里员工填写的实盘数量。"""
import logging
import os
from google.oauth2.service_account import Credentials
import gspread
from config import Config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BRANCH_TABS = {
    "antipolo": "Stock - Antipolo",
    "qc":       "Stock - QC",
}

# 列索引（0-based）
COL_PRODUCT = 1   # B: Product Name
COL_UNIT    = 2   # C: Unit
COL_COUNTED = 3   # D: Counted Qty
COL_NOTES   = 4   # E: Notes


def _get_sheet_id() -> str:
    # 优先用专用 STOCK_SHEET_ID，否则复用 Odoo Follow-up 表格
    return os.getenv("STOCK_SHEET_ID") or os.getenv("GOOGLE_SHEET_ID", "")


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(Config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def read_inventory_counts(branch: str) -> dict[str, dict]:
    """
    读取指定门店 Tab 里员工填写的实盘数量（只返回 D 列非空的行）。
    返回:
    {
      "ZNW - Rainbow Jelly 2.5kg": {"counted": 600.0, "unit": "kg", "notes": ""},
      ...
    }
    """
    tab_name = BRANCH_TABS.get(branch.lower())
    if not tab_name:
        raise ValueError(f"Unknown branch: {branch}")

    gc = _get_client()
    sh = gc.open_by_key(_get_sheet_id())
    ws = sh.worksheet(tab_name)
    rows = ws.get_all_values()

    result = {}
    for row in rows[1:]:  # 跳过表头
        # 跳过品类标题行（B列空）
        if len(row) <= COL_PRODUCT or not row[COL_PRODUCT].strip():
            continue
        if len(row) <= COL_COUNTED or not row[COL_COUNTED].strip():
            continue  # 未填写，跳过

        name    = row[COL_PRODUCT].strip()
        counted_raw = row[COL_COUNTED].strip().replace(",", "")
        notes   = row[COL_NOTES].strip() if len(row) > COL_NOTES else ""
        unit    = row[COL_UNIT].strip() if len(row) > COL_UNIT else ""

        try:
            counted = float(counted_raw)
        except ValueError:
            logger.warning("stock_sheet: cannot parse counted qty '%s' for %s", counted_raw, name)
            continue

        result[name] = {"counted": counted, "unit": unit, "notes": notes}

    return result
