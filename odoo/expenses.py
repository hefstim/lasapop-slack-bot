"""Expense (hr.expense) creation and approval workflow."""
import base64
from datetime import date

from odoo.client import get_client

DEFAULT_PRODUCT_ID = 6  # "Expenses" (generic/miscellaneous) category


def create_and_submit_expense(employee_id: int, amount: float, description: str,
                               product_id: int = DEFAULT_PRODUCT_ID,
                               payment_mode: str = "own_account") -> int:
    """Create a draft expense and submit it for approval. Returns the expense id."""
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


def approve_expense(expense_id: int):
    get_client().execute("hr.expense", "action_approve", [expense_id])


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
        ["name", "employee_id", "total_amount", "state"],
    )
    return rows[0] if rows else {}


def get_all_time_approved_total() -> dict:
    """All-time total + count of approved cash expenses (no date filter) —
    used against `cash_topups` to compute the running factory cash balance."""
    client = get_client()
    rows = client.search_read(
        "hr.expense",
        [("state", "=", "approved")],
        ["total_amount"],
    )
    return {"total": sum(r["total_amount"] for r in rows), "count": len(rows)}


def get_month_expense_summary(year: int, month: int) -> dict:
    """Total + per-employee breakdown of approved cash expenses for a given month.

    Only counts `state='approved'` — this is a spend-tracking summary against
    the monthly cash withdrawal, not a formal accounting total (approved
    expenses here never get posted to the books; see project notes).
    """
    client = get_client()
    start = date(year, month, 1).isoformat()
    end_month, end_year = (month + 1, year) if month < 12 else (1, year + 1)
    end = date(end_year, end_month, 1).isoformat()

    rows = client.search_read(
        "hr.expense",
        [("state", "=", "approved"), ("date", ">=", start), ("date", "<", end)],
        ["employee_id", "total_amount"],
    )

    by_employee: dict[str, float] = {}
    total = 0.0
    for r in rows:
        name = r["employee_id"][1] if r.get("employee_id") else "Unknown"
        by_employee[name] = by_employee.get(name, 0.0) + r["total_amount"]
        total += r["total_amount"]

    return {"total": total, "count": len(rows), "by_employee": by_employee}
