"""Newly paid invoice queries."""
from datetime import datetime, timezone
from odoo.client import get_client


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _get_payment_info(client, invoice_id: int) -> tuple[str, str, str]:
    """Return (payment method, receiving account, payment date) for a paid invoice,
    joining multiple reconciled payments (e.g. partial payments from different accounts)."""
    payments = client.search_read(
        "account.payment",
        [("reconciled_invoice_ids", "in", [invoice_id])],
        ["payment_method_line_id", "journal_id", "date"],
    )
    if not payments:
        return "—", "—", "—"

    method_line_ids = [
        p["payment_method_line_id"][0] for p in payments
        if isinstance(p.get("payment_method_line_id"), (list, tuple))
    ]
    method_names = {}
    if method_line_ids:
        lines = client.search_read(
            "account.payment.method.line", [("id", "in", method_line_ids)], ["id", "name"],
        )
        method_names = {l["id"]: l["name"] for l in lines}

    methods, accounts = [], []
    for p in payments:
        if isinstance(p.get("payment_method_line_id"), (list, tuple)):
            mid = p["payment_method_line_id"][0]
            methods.append(method_names.get(mid, p["payment_method_line_id"][1]))
        if isinstance(p.get("journal_id"), (list, tuple)):
            accounts.append(p["journal_id"][1])

    method = "、".join(dict.fromkeys(methods)) or "—"
    account = "、".join(dict.fromkeys(accounts)) or "—"
    dates = [p["date"] for p in payments if p.get("date")]
    pay_date = max(dates) if dates else "—"
    return method, account, pay_date


def get_new_payments(since_ts: str) -> list[dict]:
    """Return invoices that became fully paid after since_ts."""
    client = get_client()
    invoices = client.search_read(
        "account.move",
        [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "=", "paid"),
            ("write_date", ">", since_ts),
        ],
        ["id", "name", "partner_id", "amount_total", "currency_id",
         "invoice_user_id", "write_date", "invoice_origin"],
        order="write_date asc",
    )

    origins = [inv["invoice_origin"] for inv in invoices if inv.get("invoice_origin")]
    warehouse_by_order = {}
    order_date_by_name = {}
    if origins:
        orders = client.search_read(
            "sale.order", [("name", "in", origins)], ["name", "warehouse_id", "date_order"],
        )
        warehouse_by_order = {
            o["name"]: o["warehouse_id"][1] for o in orders
            if isinstance(o.get("warehouse_id"), (list, tuple))
        }
        order_date_by_name = {
            o["name"]: (o.get("date_order") or "")[:10] for o in orders
        }

    results = []
    for inv in invoices:
        partner = inv["partner_id"][1] if isinstance(inv.get("partner_id"), (list, tuple)) else ""
        salesperson = inv["invoice_user_id"][1] if isinstance(inv.get("invoice_user_id"), (list, tuple)) else "Unassigned"
        currency = "₱"
        if isinstance(inv.get("currency_id"), (list, tuple)):
            currency = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(inv["currency_id"][1], "₱")
        payment_method, account, pay_date = _get_payment_info(client, inv["id"])
        order_name = inv.get("invoice_origin") or "—"
        results.append({
            "invoice_id":   inv["id"],
            "invoice_name": inv["name"],
            "partner":      partner,
            "salesperson":  salesperson,
            "amount":       inv.get("amount_total", 0),
            "currency":     currency,
            "write_date":   inv.get("write_date", ""),
            "payment_method": payment_method,
            "account":      account,
            "pay_date":     pay_date,
            "order_name":   order_name,
            "order_date":   order_date_by_name.get(order_name, "—"),
            "warehouse":    warehouse_by_order.get(order_name, "—"),
        })
    return results


def get_reactivated_customers(excluded_list: list) -> list[dict]:
    """Return excluded customers who have placed a new order since being excluded."""
    if not excluded_list:
        return []
    client = get_client()
    results = []
    for exc in excluded_list:
        partner_id  = exc["partner_id"]
        excluded_at = exc["excluded_at"] or "2000-01-01 00:00:00"
        orders = client.search_read(
            "sale.order",
            [("partner_id", "=", partner_id),
             ("state", "in", ["sale", "done"]),
             ("date_order", ">", excluded_at)],
            ["name", "amount_total", "date_order", "currency_id"],
            limit=1, order="date_order desc",
        )
        if orders:
            o = orders[0]
            currency = "₱"
            if isinstance(o.get("currency_id"), (list, tuple)):
                currency = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(o["currency_id"][1], "₱")
            results.append({
                "partner_id":   partner_id,
                "partner_name": exc["partner_name"],
                "order_name":   o["name"],
                "amount":       o.get("amount_total", 0),
                "currency":     currency,
                "date":         (o.get("date_order") or "")[:10],
            })
    return results
