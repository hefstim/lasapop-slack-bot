"""New confirmed sale order queries."""
from odoo.client import get_client

DELIVERY_STATUS_LABELS = {
    "pending": "Not Delivered",
    "started": "Started",
    "partial": "Partially Delivered",
    "full": "Fully Delivered",
}


def get_max_order_id() -> int:
    client = get_client()
    orders = client.search_read(
        "sale.order", [("state", "in", ["sale", "done"])],
        ["id"], limit=1, order="id desc"
    )
    return orders[0]["id"] if orders else 0


def get_new_confirmed_orders(since_id: int) -> list[dict]:
    """Return confirmed orders with id > since_id."""
    client = get_client()
    orders = client.search_read(
        "sale.order",
        [("state", "=", "sale"), ("id", ">", since_id)],
        ["id", "name", "partner_id", "amount_total", "user_id", "date_order",
         "currency_id", "warehouse_id", "delivery_status"],
        order="id asc",
    )
    results = []
    for o in orders:
        partner = o["partner_id"][1] if isinstance(o.get("partner_id"), (list, tuple)) else ""
        salesperson = o["user_id"][1] if isinstance(o.get("user_id"), (list, tuple)) else "Unassigned"
        currency = "₱"
        if isinstance(o.get("currency_id"), (list, tuple)):
            currency = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(o["currency_id"][1], "₱")
        warehouse = o["warehouse_id"][1] if isinstance(o.get("warehouse_id"), (list, tuple)) else "—"
        delivery_status = DELIVERY_STATUS_LABELS.get(o.get("delivery_status"), "—")
        results.append({
            "order_id":   o["id"],
            "order_name": o["name"],
            "partner":    partner,
            "salesperson": salesperson,
            "amount":     o.get("amount_total", 0),
            "currency":   currency,
            "date":       (o.get("date_order") or "")[:10],
            "warehouse":  warehouse,
            "delivery_status": delivery_status,
        })
    return results
