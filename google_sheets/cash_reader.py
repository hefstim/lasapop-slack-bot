"""
读取各门店现金流 Google Sheet，返回当日支出汇总。

Sheet ID: 10r4sWc5J7-K_qa8FkiyjBRHy7eSB8GcRIR305BbKq5g
列顺序: Date | Branch | Type | Amount | Category | Flow Type | Description | Staff | Cash Balance
"""
import logging
from datetime import date
from google.oauth2.service_account import Credentials
import gspread
from config import Config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CASH_SHEET_ID = "10r4sWc5J7-K_qa8FkiyjBRHy7eSB8GcRIR305BbKq5g"

# 标准化名称 → Sheet Tab 名称
BRANCH_TABS = {
    "qc":       "QC Cash Flow",
    "antipolo": "Antipolo Cash Flow",
    "bulacan":  "Bulacan Cash Flow",
}

# Slack 频道里员工可能用的别名
BRANCH_ALIASES = {
    "qc": ["qc", "quezon", "quezon city"],
    "antipolo": ["antipolo"],
    "bulacan": ["bulacan"],
}


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(Config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _parse_amount(raw: str) -> float:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _date_keys(d: date) -> list[str]:
    """返回 Sheet 里可能出现的日期字符串（不补零 + 补零两种）。"""
    return [
        f"{d.year}-{d.month}-{d.day}",   # 2026-7-3
        d.strftime("%Y-%m-%d"),            # 2026-07-03
    ]


def get_daily_expenses(branch: str, target_date: date | None = None) -> dict:
    """
    读取指定门店某天的所有 Operating Expense 明细。
    返回:
        {
          "branch": "qc",
          "date":   "2026-07-03",
          "total":  1250.0,
          "count":  5,
          "by_category": {"Delivery": 700.0, "Utilities": 550.0},
          "entries": [...],
          "found":  True/False   # Sheet 里是否有当天数据
        }
    """
    if target_date is None:
        target_date = date.today()

    branch = branch.lower()
    tab = BRANCH_TABS.get(branch)
    if not tab:
        return {"branch": branch, "error": f"Unknown branch: {branch}"}

    date_keys = set(_date_keys(target_date))

    try:
        gc = _get_client()
        sh = gc.open_by_key(CASH_SHEET_ID)
        ws = sh.worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        logger.error("cash_reader: failed to read Sheet for %s: %s", branch, e)
        return {"branch": branch, "error": str(e), "found": False}

    entries = []
    by_category: dict[str, float] = {}
    total = 0.0
    has_any_today = False

    for row in rows[1:]:  # 跳过表头
        if len(row) < 6:
            continue
        row_date = row[0].strip()
        if row_date not in date_keys:
            continue
        has_any_today = True

        row_type = row[2].strip().lower()
        flow_type = row[5].strip().lower()

        # 只统计 Operating Expense，排除 Cash Movement（内部调拨）
        if row_type != "expense" or flow_type == "cash movement":
            continue

        amount = _parse_amount(row[3])
        if amount <= 0:
            continue

        category    = row[4].strip() or "Other"
        description = row[6].strip() if len(row) > 6 else ""
        staff       = row[7].strip() if len(row) > 7 else ""

        entries.append({
            "category":    category,
            "description": description,
            "amount":      amount,
            "staff":       staff,
        })
        by_category[category] = by_category.get(category, 0.0) + amount
        total += amount

    return {
        "branch":      branch,
        "date":        target_date.isoformat(),
        "total":       total,
        "count":       len(entries),
        "by_category": by_category,
        "entries":     entries,
        "found":       has_any_today,
    }


def get_daily_cashflow(branch: str, target_date: date | None = None) -> dict:
    """
    读取指定门店某天所有现金流条目（收入 + 支出 + 调拨）。
    返回:
        {
          "branch":    "antipolo",
          "date":      "2026-07-03",
          "income":    5320.0,   # Operating Income
          "expenses":  1850.0,   # Operating Expense
          "mvt_in":    0.0,      # Cash Movement 收入
          "mvt_out":   0.0,      # Cash Movement 支出
          "found":     True,
        }
    """
    if target_date is None:
        target_date = date.today()

    branch = branch.lower()
    tab = BRANCH_TABS.get(branch)
    if not tab:
        return {"branch": branch, "error": f"Unknown branch: {branch}"}

    date_keys = set(_date_keys(target_date))
    try:
        gc = _get_client()
        sh = gc.open_by_key(CASH_SHEET_ID)
        ws = sh.worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        logger.error("cash_reader: failed to read Sheet for %s: %s", branch, e)
        return {"branch": branch, "error": str(e), "found": False}

    income = expenses = mvt_in = mvt_out = 0.0
    found = False
    for row in rows[1:]:
        if len(row) < 6 or row[0].strip() not in date_keys:
            continue
        found = True
        amount    = _parse_amount(row[3])
        row_type  = row[2].strip().lower()
        flow_type = row[5].strip().lower()
        if flow_type == "operating":
            if row_type == "income":
                income += amount
            elif row_type == "expense":
                expenses += amount
        elif flow_type == "cash movement":
            if row_type == "income":
                mvt_in += amount
            else:
                mvt_out += amount

    return {
        "branch":   branch,
        "date":     target_date.isoformat(),
        "income":   income,
        "expenses": expenses,
        "mvt_in":   mvt_in,
        "mvt_out":  mvt_out,
        "found":    found,
    }


def get_all_branches_summary(target_date: date | None = None) -> list[dict]:
    """读取三个门店的当日汇总，按 QC / Antipolo / Bulacan 顺序返回。"""
    if target_date is None:
        target_date = date.today()
    return [get_daily_expenses(b, target_date) for b in BRANCH_TABS]


def detect_branch(text: str) -> str | None:
    """从员工 Slack 消息里识别门店名称。"""
    text_lower = text.lower()
    for branch, aliases in BRANCH_ALIASES.items():
        for alias in aliases:
            if alias in text_lower:
                return branch
    return None


def _parse_balance(raw: str) -> float | None:
    """解析 Cash Balance 列的值，去掉 ₱、逗号等格式字符。"""
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.replace("₱", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _read_tab_rows(branch: str) -> list[list[str]]:
    """读取指定门店 Tab 的所有行（含表头）。"""
    tab = BRANCH_TABS.get(branch.lower())
    if not tab:
        return []
    try:
        gc = _get_client()
        sh = gc.open_by_key(CASH_SHEET_ID)
        ws = sh.worksheet(tab)
        return ws.get_all_values()
    except Exception as e:
        logger.error("cash_reader._read_tab_rows(%s): %s", branch, e)
        return []


def get_closing_balance(branch: str, target_date: date) -> float | None:
    """
    获取指定门店在 target_date 当天最后一行的 Cash Balance。
    如果当天没有 Cash Balance 数据，返回 None。
    """
    rows = _read_tab_rows(branch)
    date_keys = set(_date_keys(target_date))
    last_balance = None
    for row in rows[1:]:
        if len(row) < 9:
            continue
        if row[0].strip() not in date_keys:
            continue
        bal = _parse_balance(row[8])
        if bal is not None:
            last_balance = bal
    return last_balance


def _parse_sheet_date(raw: str) -> date | None:
    """解析 Sheet 日期列（兼容 2026-7-3 和 2026-07-03 两种格式）。"""
    if not raw or "-" not in raw:
        return None
    try:
        parts = raw.strip().split("-")
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        pass
    return None


def get_previous_closing_balance(branch: str, before_date: date) -> float | None:
    """
    获取 before_date 之前最近一天的 Cash Balance（作为今天的期初结余）。
    """
    rows = _read_tab_rows(branch)
    daily: dict[date, float] = {}
    for row in rows[1:]:
        if len(row) < 9:
            continue
        bal = _parse_balance(row[8])
        if bal is None:
            continue
        d = _parse_sheet_date(row[0].strip())
        if d and d < before_date:
            daily[d] = bal  # 同一天多行，最后一行覆盖前面的

    if not daily:
        return None
    return daily[max(daily.keys())]


def get_cash_movements(branch: str, target_date: date) -> float:
    """
    获取当天内部调拨（Flow Type = Cash Movement）的净额。
    收入型调拨（Type=Income）为正，支出型（Type=Expense）为负。
    """
    rows = _read_tab_rows(branch)
    date_keys = set(_date_keys(target_date))
    net = 0.0
    for row in rows[1:]:
        if len(row) < 6:
            continue
        if row[0].strip() not in date_keys:
            continue
        if row[5].strip().lower() != "cash movement":
            continue
        amount = _parse_amount(row[3])
        if row[2].strip().lower() == "expense":
            net -= amount
        else:
            net += amount
    return net
