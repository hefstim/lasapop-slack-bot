"""Dormant customer queries."""
from datetime import datetime, timedelta
from config import Config
from odoo.client import get_client
import db


def get_dormant_customers() -> list[dict]:
    """Return customers who have ordered before but not within DORMANT_CUSTOMER_DAYS."""
    client = get_client()
    cutoff = (datetime.now() - timedelta(days=Config.DORMANT_CUSTOMER_DAYS)).strftime("%Y-%m-%d 00:00:00")

    # Partners that ordered recently (within the window)
    recent_orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]), ("date_order", ">=", cutoff)],
        ["partner_id"],
    )
    active_partner_ids = {o["partner_id"][0] for o in recent_orders}

    # Only customers who have ever placed a confirmed order
    ever_ordered = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"])],
        ["partner_id"],
    )
    ever_ordered_ids = list({o["partner_id"][0] for o in ever_ordered})

    if not ever_ordered_ids:
        return []

    all_customers = client.search_read(
        "res.partner",
        [("id", "in", ever_ordered_ids), ("active", "=", True)],
        ["id", "name", "email", "phone", "x_viber", "x_facebook", "user_id"],
    )

    excluded_ids = db.get_excluded_partner_ids()
    dormant_ids = [
        c["id"] for c in all_customers
        if c["id"] not in active_partner_ids and c["id"] not in excluded_ids
    ]
    if not dormant_ids:
        return []

    # Last order date + total amount per dormant customer
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

    # Order count per dormant customer
    all_orders = client.search_read(
        "sale.order",
        [("partner_id", "in", dormant_ids), ("state", "in", ["sale", "done"])],
        ["partner_id"],
    )
    order_count_map: dict[int, int] = {}
    for o in all_orders:
        pid = o["partner_id"][0]
        order_count_map[pid] = order_count_map.get(pid, 0) + 1

    now = datetime.now()
    results = []
    for c in all_customers:
        if c["id"] not in dormant_ids:
            continue

        info = last_order_map.get(c["id"], {})
        last_date_raw = info.get("last_order_date")

        days_inactive = Config.DORMANT_CUSTOMER_DAYS
        last_date_str = "Never"
        if last_date_raw:
            try:
                last_dt = datetime.strptime(last_date_raw[:10], "%Y-%m-%d")
                days_inactive = (now - last_dt).days
                last_date_str = last_dt.strftime("%b %d, %Y")
            except ValueError:
                pass

        salesperson = ""
        if c.get("user_id") and isinstance(c["user_id"], (list, tuple)):
            salesperson = c["user_id"][1]

        phone   = c.get("phone") or ""
        viber   = c.get("x_viber") or ""
        facebook = c.get("x_facebook") or ""

        results.append({
            "partner_id": c["id"],
            "name": c["name"],
            "email": c.get("email") or "",
            "phone": phone,
            "viber": viber,
            "facebook": facebook,
            "salesperson": salesperson,
            "days_inactive": days_inactive,
            "last_order_date": last_date_str,
            "last_order_amount": info.get("last_order_amount", 0),
            "order_count": order_count_map.get(c["id"], 0),
        })

    results.sort(key=lambda x: x["days_inactive"], reverse=True)
    return results


def get_new_customers_missing_contact(since_ts: str) -> list[dict]:
    """New customers (customer_rank > 0) created since since_ts that are
    missing at least one of phone/email/Facebook/Viber."""
    client = get_client()
    partners = client.search_read(
        "res.partner",
        [("customer_rank", ">", 0), ("create_date", ">", since_ts)],
        ["id", "name", "phone", "email", "x_facebook", "x_viber"],
    )

    results = []
    for p in partners:
        missing = [
            label for label, val in [
                ("Phone", p.get("phone")), ("Email", p.get("email")),
                ("Facebook", p.get("x_facebook")), ("Viber", p.get("x_viber")),
            ] if not val
        ]
        if missing:
            results.append({
                "partner_id": p["id"],
                "name": p["name"],
                "missing": missing,
            })
    return results
