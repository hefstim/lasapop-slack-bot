"""Expense (hr.expense) creation and approval workflow."""
import base64
from datetime import date

from config import Config
from odoo.client import get_client

DEFAULT_PRODUCT_ID = 6  # "Expenses" (generic/miscellaneous) category

# Petty cash expense categories: expense products created by petty_cash_setup.py,
# each mapped to its own account. Keyed by the product's default_code.
EXPENSE_CATEGORIES = [
    ("EXP-FREIGHT", "运费 / Freight"),
    ("EXP-TRANSPORT", "交通燃油 / Transport & Fuel"),
    ("EXP-REPAIR", "配件耗材 / Parts & Supplies"),
    ("EXP-UTIL", "水电燃气 / Utilities"),
    ("EXP-MEAL", "餐饮 / Meals"),
    ("EXP-OFFICE", "办公网络 / Office & Internet"),
    ("EXP-PERMIT", "证照 / Permits"),
    ("EXP-LABOR", "人工 / Labor"),
    ("EXP-EQUIP", "设备（固定资产）/ Equipment (asset)"),
    ("EXP-RENO", "装修（固定资产）/ Renovation (asset)"),
    ("EXP-OTHER", "其他 / Other"),
]
CATEGORY_LABELS = dict(EXPENSE_CATEGORIES)
# Odoo states that mean the money has been spent: approved (original flow, never
# posted) or paid (petty cash flow, posted on approval).
SPENT_STATES = ["approved", "paid", "in_payment"]


def category_product_id(code: str) -> int:
    rows = get_client().search_read("product.product", [("default_code", "=", code)], ["id"], limit=1)
    if not rows:
        raise RuntimeError(f"expense category {code} not found in Odoo (run petty_cash_setup.py)")
    return rows[0]["id"]


def create_and_submit_expense(employee_id: int, amount: float, description: str,
                               product_id: int = DEFAULT_PRODUCT_ID,
                               payment_mode: str = "own_account",
                               category_code: str = "") -> int:
    """Create a draft expense and submit it for approval. Returns the expense id.

    With petty cash enabled the expense is company-paid (from the petty cash
    journal) and, if given, uses the category's product.
    """
    if Config.PETTY_CASH_ENABLED:
        payment_mode = "company_account"
        if category_code:
            product_id = category_product_id(category_code)
    client = get_client()
    expense_id = client.execute("hr.expense", "create", {
        "name": description,
        "employee_id": employee_id,
        "product_id": product_id,
        "total_amount": amount,
        "payment_mode": payment_mode,
        "date": date.today().isoformat(),
    })
    client.execute("hr.expense", "action_submit", [expense_id])
    return expense_id


def set_expense_category(expense_id: int, category_code: str):
    """Re-categorise an expense before approval (the approver picks it on the card)."""
    get_client().execute("hr.expense", "write", [expense_id], {"product_id": category_product_id(category_code)})


class DuplicateExpenseError(Exception):
    """Odoo flagged the expense as a possible duplicate (same employee, date and
    amount as another expense) and wants a human to confirm before approving."""

    def __init__(self, expense_id: int, duplicates: list):
        self.expense_id = expense_id
        self.duplicates = duplicates
        super().__init__(f"expense {expense_id} may duplicate {[d['id'] for d in duplicates]}")


def approve_expense(expense_id: int, confirm_duplicate: bool = False):
    """Approve an expense, raising unless Odoo really ends up approving it.

    For a possible duplicate, action_approve doesn't raise — it returns the
    hr.expense.approve.duplicate wizard and leaves the expense submitted. Raise
    DuplicateExpenseError so the approver can check it, or, with
    confirm_duplicate, approve through that wizard (it approves only the
    expenses it is given).

    A company-paid (petty cash) expense is then posted, which pays it from the
    petty cash journal; it must end up "paid" instead of "approved".
    """
    client = get_client()
    # Employees without an expense approver are auto-approved by Odoo on submit;
    # calling action_approve again would only pop the duplicate wizard for nothing.
    if get_expense(expense_id).get("state") == "submitted":
        result = client.execute("hr.expense", "action_approve", [expense_id])
        if isinstance(result, dict) and result.get("res_model") == "hr.expense.approve.duplicate":
            if not confirm_duplicate:
                raise DuplicateExpenseError(expense_id, get_duplicate_expenses(expense_id))
            wizard_id = client.execute("hr.expense.approve.duplicate", "create",
                                       {"expense_ids": [(6, 0, [expense_id])]})
            client.execute("hr.expense.approve.duplicate", "action_approve", [wizard_id])
    expense = get_expense(expense_id)
    company_paid = expense.get("payment_mode") == "company_account"
    if company_paid and expense.get("state") == "approved":
        client.execute("hr.expense", "action_post", [expense_id])
        expense = get_expense(expense_id)
    expected = "paid" if company_paid else "approved"
    if expense.get("state") != expected:
        raise RuntimeError(f"Odoo did not {'post' if company_paid else 'approve'} expense {expense_id} "
                           f"(state: {expense.get('state')})")


def get_duplicate_expenses(expense_id: int) -> list:
    """The other expenses Odoo considers duplicates of this one."""
    client = get_client()
    rows = client.search_read("hr.expense", [("id", "=", expense_id)], ["duplicate_expense_ids"])
    other_ids = [i for i in (rows[0]["duplicate_expense_ids"] if rows else []) if i != expense_id]
    if not other_ids:
        return []
    return client.search_read("hr.expense", [("id", "in", other_ids)],
                              ["name", "date", "total_amount", "state"])


def refuse_expense(expense_id: int, reason: str):
    """Refuse an expense via the hr.expense.refuse.wizard (action_refuse on
    hr.expense itself only opens the reason dialog — it doesn't refuse)."""
    client = get_client()
    wizard_id = client.execute("hr.expense.refuse.wizard", "create", {
        "reason": reason,
        "expense_ids": [(6, 0, [expense_id])],
    })
    client.execute("hr.expense.refuse.wizard", "action_refuse", [wizard_id])


def attach_receipt(expense_id: int, filename: str, file_bytes: bytes) -> int:
    """Attach a receipt image/file to an expense (visible on the expense form in Odoo)."""
    client = get_client()
    return client.execute("ir.attachment", "create", {
        "name": filename,
        "res_model": "hr.expense",
        "res_id": expense_id,
        "datas": base64.b64encode(file_bytes).decode("ascii"),
    })


def get_expense(expense_id: int) -> dict:
    client = get_client()
    rows = client.search_read(
        "hr.expense",
        [("id", "=", expense_id)],
        ["name", "employee_id", "total_amount", "state", "payment_mode"],
    )
    return rows[0] if rows else {}


def get_all_time_approved_total() -> dict:
    """All-time total + count of approved cash expenses (no date filter) —
    used against `cash_topups` to compute the running factory cash balance
    when petty cash is off."""
    client = get_client()
    rows = client.search_read(
        "hr.expense",
        [("state", "in", SPENT_STATES)],
        ["total_amount"],
    )
    return {"total": sum(r["total_amount"] for r in rows), "count": len(rows)}


def get_petty_cash_balance() -> float:
    """Balance of the petty cash account from posted journal entries (top-ups are
    booked by the AI assistant, expenses by this bot)."""
    client = get_client()
    accounts = client.search_read("account.account", [("code", "=", Config.PETTY_CASH_ACCOUNT)], ["id"], limit=1)
    if not accounts:
        raise RuntimeError(f"account {Config.PETTY_CASH_ACCOUNT} not found in Odoo")
    rows = client.search_read(
        "account.move.line",
        [("account_id", "=", accounts[0]["id"]), ("parent_state", "=", "posted")],
        ["balance"],
    )
    return sum(r["balance"] for r in rows)


def get_month_expense_summary(year: int, month: int) -> dict:
    """Total + per-employee and per-category breakdown of spent cash expenses for a month.

    Counts approved (original flow) and paid (petty cash flow) expenses.
    """
    client = get_client()
    start = date(year, month, 1).isoformat()
    end_month, end_year = (month + 1, year) if month < 12 else (1, year + 1)
    end = date(end_year, end_month, 1).isoformat()

    rows = client.search_read(
        "hr.expense",
        [("state", "in", SPENT_STATES), ("date", ">=", start), ("date", "<", end)],
        ["employee_id", "product_id", "total_amount"],
    )

    by_employee: dict[str, float] = {}
    by_category: dict[str, float] = {}
    total = 0.0
    for r in rows:
        name = r["employee_id"][1] if r.get("employee_id") else "Unknown"
        by_employee[name] = by_employee.get(name, 0.0) + r["total_amount"]
        # display name is "[EXP-FREIGHT] 运费 / Freight"; drop the code prefix
        category = r["product_id"][1].split("] ", 1)[-1] if r.get("product_id") else "Unknown"
        by_category[category] = by_category.get(category, 0.0) + r["total_amount"]
        total += r["total_amount"]

    return {"total": total, "count": len(rows), "by_employee": by_employee, "by_category": by_category}
