"""Overdue invoice queries."""
from datetime import date, datetime
from config import Config
from odoo.client import get_client


def _query_overdue_invoices() -> list[dict]:
    """Return all unpaid/partial posted invoices past due date, with computed fields."""
    client = get_client()
    today_str = date.today().isoformat()

    invoices = client.search_read(
        "account.move",
        [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
            ("invoice_date_due", "<", today_str),
        ],
        ["name", "partner_id", "invoice_date_due", "amount_residual", "currency_id",
         "invoice_user_id"],
        order="invoice_date_due asc",
    )

    today = date.today()
    results = []
    for inv in invoices:
        due_raw = inv.get("invoice_date_due") or ""
        days_overdue = 0
        due_str = due_raw
        if due_raw:
            try:
                due_dt = datetime.strptime(due_raw, "%Y-%m-%d").date()
                days_overdue = (today - due_dt).days
                due_str = due_dt.strftime("%Y年%m月%d日")
            except ValueError:
                pass

        partner_name = ""
        if inv.get("partner_id") and isinstance(inv["partner_id"], (list, tuple)):
            partner_name = inv["partner_id"][1]

        salesperson = ""
        if inv.get("invoice_user_id") and isinstance(inv["invoice_user_id"], (list, tuple)):
            salesperson = inv["invoice_user_id"][1]

        currency = "¥"
        if inv.get("currency_id") and isinstance(inv["currency_id"], (list, tuple)):
            symbol_map = {"CNY": "¥", "USD": "$", "EUR": "€"}
            currency = symbol_map.get(inv["currency_id"][1], inv["currency_id"][1] + " ")

        results.append({
            "invoice_id": inv["id"],
            "invoice_name": inv["name"],
            "partner_name": partner_name,
            "salesperson": salesperson,
            "amount_residual": inv.get("amount_residual", 0),
            "currency": currency,
            "due_date": due_str,
            "days_overdue": days_overdue,
        })

    return results


def get_overdue_invoices() -> list[dict]:
    """Recently overdue invoices — within the daily Slack reminder window."""
    return [
        inv for inv in _query_overdue_invoices()
        if Config.OVERDUE_INVOICE_DAYS <= inv["days_overdue"] <= Config.OVERDUE_INVOICE_MAX_DAYS
    ]


def get_legacy_overdue_invoices() -> list[dict]:
    """Stale overdue invoices beyond the daily reminder window — for manual
    one-by-one payment verification (likely data entry errors, not bad debt)."""
    return [
        inv for inv in _query_overdue_invoices()
        if inv["days_overdue"] > Config.OVERDUE_INVOICE_MAX_DAYS
    ]
