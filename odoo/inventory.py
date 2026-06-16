"""Low stock queries."""
from config import Config
from odoo.client import get_client


def get_low_stock_products() -> list[dict]:
    """Return storable products whose on-hand qty is below the reorder point or threshold."""
    client = get_client()

    # Try to use stock.warehouse.orderpoint (reorder rules) first
    orderpoints = client.search_read(
        "stock.warehouse.orderpoint",
        [("active", "=", True)],
        ["product_id", "product_min_qty", "qty_on_hand", "warehouse_id"],
    )

    results = []

    if orderpoints:
        for op in orderpoints:
            qty = op.get("qty_on_hand", 0) or 0
            min_qty = op.get("product_min_qty", 0) or 0
            if qty <= min_qty:
                product_name = ""
                if op.get("product_id") and isinstance(op["product_id"], (list, tuple)):
                    product_name = op["product_id"][1]
                warehouse_name = ""
                if op.get("warehouse_id") and isinstance(op["warehouse_id"], (list, tuple)):
                    warehouse_name = op["warehouse_id"][1]

                results.append({
                    "product_id": op["product_id"][0] if isinstance(op["product_id"], (list, tuple)) else 0,
                    "product_name": product_name,
                    "qty_on_hand": round(qty, 2),
                    "reorder_point": round(min_qty, 2),
                    "warehouse": warehouse_name,
                })
    else:
        # Fall back to global threshold
        quants = client.search_read(
            "stock.quant",
            [
                ("location_id.usage", "=", "internal"),
                ("quantity", "<=", Config.LOW_STOCK_THRESHOLD),
            ],
            ["product_id", "quantity", "location_id"],
        )
        for q in quants:
            product_name = ""
            if q.get("product_id") and isinstance(q["product_id"], (list, tuple)):
                product_name = q["product_id"][1]
            location = ""
            if q.get("location_id") and isinstance(q["location_id"], (list, tuple)):
                location = q["location_id"][1]

            results.append({
                "product_id": q["product_id"][0] if isinstance(q["product_id"], (list, tuple)) else 0,
                "product_name": product_name,
                "qty_on_hand": round(q.get("quantity", 0), 2),
                "reorder_point": Config.LOW_STOCK_THRESHOLD,
                "warehouse": location,
            })

    results.sort(key=lambda x: x["qty_on_hand"])
    return results
