"""Dormant customer queries."""
from datetime import datetime, timedelta
from config import Config
from odoo.client import get_client
import db


def get_dormant_customers() -> list[dict]:
    """Return customers with no confirmed sale order in the last DORMANT_CUSTOMER_DAYS days."""
    client = get_client()
    cutoff = (datetime.now() - timedelta(days=Config.DORMANT_CUSTOMER_DAYS)).strftime("%Y-%m-%d 00:00:00")

    # Partners that ordered recently
    recent_orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]), ("date_order", ">=", cutoff)],
        ["partner_id"],
    )
    active_partner_ids = {o["partner_id"][0] for o in recent_orders}

    # All customers that have at least one confirmed order ever
    ever_ordered_ids = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"])],
        ["partner_id"],
    )
    ever_ordered_partner_ids = list({o["partner_id"][0] for o in ever_ordered_ids})

    if not ever_ordered_partner_ids:
        return []

    all_customers = client.search_read(
        "res.partner",
        [("id", "in", ever_ordered_partner_ids), ("active", "=", True)],
        ["id", "name", "email", "phone", "user_id"],
    )

    excluded_ids = db.get_excluded_partner_ids()
    dormant_ids = [c["id"] for c in all_customers if c["id"] not in active_partner_ids and c["id"] not in excluded_ids]
    if not dormant_ids:
        return []

    # Get last order date per dormant customer via read_group
    groups = client.read_group(
        "sale.order",
        [("partner_id", "in", dormant_ids), ("state", "in", ["sale", "done"])],
        ["partner_id", "date_order:max", "amount_total:sum"],
        ["partner_id"],
    )
    last_order_map = {
        g["partner_id"][0]: {
            "last_order_date": g.get("date_order:max") or g.get("date_order"),
            "last_order_amount": g.get("amount_total:sum") or g.get("amount_total") or 0,
        }
        for g in groups
        if g.get("partner_id")
    }

    now = datetime.now()
    results = []
    for c in all_customers:
        if c["id"] not in dormant_ids:
            continue
        info = last_order_map.get(c["id"], {})
        last_date_raw = info.get("last_order_date")

        days_inactive = Config.DORMANT_CUSTOMER_DAYS  # default: never ordered within window
        last_date_str = "Never purchased"
        if last_date_raw:
            # Odoo returns datetime string like "2025-12-10 08:00:00"
            try:
                last_dt = datetime.strptime(last_date_raw[:10], "%Y-%m-%d")
                days_inactive = (now - last_dt).days
                last_date_str = last_dt.strftime("%Y年%m月%d日")
            except ValueError:
                pass

        salesperson = ""
        if c.get("user_id") and isinstance(c["user_id"], (list, tuple)):
            salesperson = c["user_id"][1]

        results.append({
            "partner_id": c["id"],
            "name": c["name"],
            "email": c.get("email") or "",
            "phone": c.get("phone") or "",
            "salesperson": salesperson,
            "days_inactive": days_inactive,
            "last_order_date": last_date_str,
            "last_order_amount": info.get("last_order_amount", 0),
        })

    # Sort by most inactive first
    results.sort(key=lambda x: x["days_inactive"], reverse=True)
    return results
