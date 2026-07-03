"""查询 Odoo 各门店当前库存。"""
import logging
from odoo.client import get_client

logger = logging.getLogger(__name__)

BRANCH_LOCATIONS = {
    "antipolo": 8,
    "qc":       18,
    "bulacan":  42,
}


def get_odoo_stock(branch: str) -> dict[str, dict]:
    """
    返回指定门店 Odoo 当前库存。
    {
      "ZNW - Rainbow Jelly 2.5kg": {"qty": 646.0, "unit": "kg"},
      ...
    }
    """
    branch = branch.lower()
    loc_id = BRANCH_LOCATIONS.get(branch)
    if not loc_id:
        return {}

    client = get_client()
    quants = client.search_read(
        "stock.quant",
        [("location_id", "=", loc_id)],
        ["product_id", "quantity"],
    )

    prod_ids = list({q["product_id"][0] for q in quants})
    if not prod_ids:
        return {}

    products = client.search_read(
        "product.product",
        [("id", "in", prod_ids)],
        ["id", "name", "uom_id"],
    )
    prod_info = {p["id"]: p for p in products}

    result: dict[str, dict] = {}
    for q in quants:
        pid  = q["product_id"][0]
        info = prod_info.get(pid, {})
        name = info.get("name") or q["product_id"][1]
        unit = info["uom_id"][1] if isinstance(info.get("uom_id"), list) else ""
        result[name] = {
            "qty":  result.get(name, {}).get("qty", 0) + q["quantity"],
            "unit": unit,
        }

    return result
