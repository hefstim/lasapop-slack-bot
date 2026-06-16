"""Quotation follow-up queries — quotes sent but not confirmed."""
from datetime import datetime, timedelta
from config import Config
from odoo.client import get_client


def get_pending_quotations() -> list[dict]:
    """Return quotations in 'sent' state older than QUOTATION_FOLLOWUP_DAYS."""
    client = get_client()
    cutoff = (datetime.now() - timedelta(days=Config.QUOTATION_FOLLOWUP_DAYS)).strftime(
        "%Y-%m-%d 00:00:00"
    )

    orders = client.search_read(
        "sale.order",
        [("state", "=", "sent"), ("date_order", "<=", cutoff)],
        ["name", "partner_id", "date_order", "amount_total", "user_id", "currency_id"],
        order="date_order asc",
    )

    now = datetime.now()
    results = []
    for o in orders:
        date_raw = o.get("date_order") or ""
        days_pending = Config.QUOTATION_FOLLOWUP_DAYS
        create_date_str = ""
        if date_raw:
            try:
                dt = datetime.strptime(date_raw[:19], "%Y-%m-%d %H:%M:%S")
                days_pending = (now - dt).days
                create_date_str = dt.strftime("%Y年%m月%d日")
            except ValueError:
                pass

        partner_name = ""
        if o.get("partner_id") and isinstance(o["partner_id"], (list, tuple)):
            partner_name = o["partner_id"][1]

        salesperson = ""
        if o.get("user_id") and isinstance(o["user_id"], (list, tuple)):
            salesperson = o["user_id"][1]

        currency = "¥"
        if o.get("currency_id") and isinstance(o["currency_id"], (list, tuple)):
            symbol_map = {"CNY": "¥", "USD": "$", "EUR": "€"}
            currency = symbol_map.get(o["currency_id"][1], o["currency_id"][1] + " ")

        results.append({
            "order_id": o["id"],
            "order_name": o["name"],
            "partner_name": partner_name,
            "salesperson": salesperson,
            "amount_total": o.get("amount_total", 0),
            "currency": currency,
            "create_date": create_date_str,
            "days_pending": days_pending,
        })

    return results
