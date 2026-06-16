"""Data integrity checks — surfaces likely operator mistakes in Odoo as they happen."""
from datetime import date, timedelta
from odoo.client import get_client


def check_invoice_anomalies(since_ts: str) -> list[dict]:
    """Inspect posted customer invoices created/modified since since_ts for
    zero/negative amounts, due date before invoice date, payment_state
    inconsistent with the actual outstanding amount, and same-order
    duplicates (same invoice_origin + same amount as an existing invoice)."""
    client = get_client()
    invoices = client.search_read(
        "account.move",
        [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("write_date", ">", since_ts),
        ],
        ["id", "name", "partner_id", "amount_total", "amount_residual",
         "payment_state", "invoice_date", "invoice_date_due", "invoice_origin"],
    )

    issues = []
    for inv in invoices:
        partner = inv["partner_id"][1] if isinstance(inv.get("partner_id"), (list, tuple)) else ""
        name = inv["name"] or inv.get("invoice_origin") or f"id={inv['id']}"

        if inv["amount_total"] <= 0:
            issues.append({
                "type": "Invalid amount",
                "detail": f"{name} ({partner}) has an invoice amount of {inv['amount_total']}",
            })

        if inv.get("invoice_date") and inv.get("invoice_date_due") and inv["invoice_date_due"] < inv["invoice_date"]:
            issues.append({
                "type": "Date logic error",
                "detail": f"{name} ({partner}) due date {inv['invoice_date_due']} is before invoice date {inv['invoice_date']}",
            })

        if inv["payment_state"] == "paid" and abs(inv["amount_residual"]) > 0.01:
            issues.append({
                "type": "Payment status mismatch",
                "detail": f"{name} ({partner}) is marked paid but still has an outstanding balance of {inv['amount_residual']}",
            })
        elif inv["payment_state"] == "not_paid" and abs(inv["amount_residual"]) < 0.01 and inv["amount_total"] != 0:
            issues.append({
                "type": "Payment status mismatch",
                "detail": f"{name} ({partner}) is marked not paid but the outstanding balance is already 0",
            })

        if inv.get("invoice_origin"):
            siblings = client.search_read(
                "account.move",
                [("invoice_origin", "=", inv["invoice_origin"]),
                 ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                 ("id", "!=", inv["id"]),
                 ("amount_total", "=", inv["amount_total"])],
                ["name"],
            )
            if siblings:
                sibling_names = ", ".join(s["name"] or str(s["id"]) for s in siblings)
                issues.append({
                    "type": "Possible duplicate invoice",
                    "detail": f"Order {inv['invoice_origin']} already has invoice(s) {sibling_names} with the same amount ({inv['amount_total']}); new invoice {name} matches it too — please confirm it isn't a duplicate",
                })

    return issues


def check_newly_stale_orders(already_alerted: set[int]) -> list[dict]:
    """Confirmed sale orders that have just crossed the 30-day-uninvoiced
    threshold and haven't been alerted on before."""
    client = get_client()
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    orders = client.search_read(
        "sale.order",
        [("state", "=", "sale"), ("invoice_status", "=", "to invoice"),
         ("date_order", "<", cutoff), ("amount_total", ">", 0)],
        ["id", "name", "partner_id", "amount_total", "date_order"],
    )
    new_stale = [o for o in orders if o["id"] not in already_alerted]

    issues = []
    for o in new_stale:
        partner = o["partner_id"][1] if isinstance(o.get("partner_id"), (list, tuple)) else ""
        issues.append({
            "type": "Order uninvoiced for 30+ days",
            "detail": f"{o['name']} ({partner}) ordered on {o['date_order'][:10]}, amount {o['amount_total']}, still not invoiced after 30+ days",
            "order_id": o["id"],
        })
    return issues
