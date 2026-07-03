"""
从 Odoo 查询当日现金收款，按门店汇总。
链路：CSH1 收款 → memo(发票号) → invoice_origin → 销售订单 → warehouse_id → 门店
"""
import logging
from datetime import date

from odoo.client import get_client

logger = logging.getLogger(__name__)

CASH_JOURNAL_ID = 6  # CSH1

# Odoo warehouse_id → Sheet branch key
WAREHOUSE_BRANCH = {
    1: "antipolo",
    2: "qc",       # Quezon-Novaliches
    4: "bulacan",
    # id=3 SCL Distributions 不在门店 Sheet 里，归入 unknown
}


def get_cash_income_by_branch(target_date: date | None = None) -> dict:
    """
    返回指定日期各门店现金收款合计及明细。
    返回格式:
    {
      "antipolo": {"total": 5320.0, "payments": [{"memo": "INV/...", "amount": 3182.0, "order": "S11139"}, ...]},
      "qc":       {"total": 2025.0, "payments": [...]},
      "bulacan":  {"total": 200.0,  "payments": [...]},
      "unknown":  {"total": 0.0,    "payments": []},   # 无法追溯仓库的
    }
    """
    if target_date is None:
        target_date = date.today()

    client = get_client()
    date_str = target_date.isoformat()

    # Step 1: 取当天所有已过账的 CSH1 收款
    payments = client.search_read(
        "account.payment",
        [
            ("journal_id", "=", CASH_JOURNAL_ID),
            ("date", "=", date_str),
            ("state", "=", "paid"),
            ("payment_type", "=", "inbound"),
        ],
        ["id", "amount", "memo"],
    )
    if not payments:
        return {}

    # Step 2: 批量查发票（memo 里是发票号）
    inv_names = [p["memo"] for p in payments if p.get("memo")]
    if inv_names:
        invoices = client.search_read(
            "account.move",
            [("name", "in", inv_names)],
            ["name", "invoice_origin"],
        )
        inv_to_origin = {inv["name"]: (inv.get("invoice_origin") or "") for inv in invoices}
    else:
        inv_to_origin = {}

    # Step 3: 批量查销售订单，拿 warehouse_id
    order_names = [o for o in inv_to_origin.values() if o]
    if order_names:
        orders = client.search_read(
            "sale.order",
            [("name", "in", order_names)],
            ["name", "warehouse_id"],
        )
        order_to_wh = {}
        for o in orders:
            if isinstance(o.get("warehouse_id"), (list, tuple)):
                order_to_wh[o["name"]] = o["warehouse_id"][0]
    else:
        order_to_wh = {}

    # Step 4: 汇总，按门店分组
    result: dict[str, dict] = {}
    for p in payments:
        memo   = p.get("memo") or ""
        origin = inv_to_origin.get(memo, "")
        wh_id  = order_to_wh.get(origin)
        branch = WAREHOUSE_BRANCH.get(wh_id, "unknown") if wh_id else "unknown"

        if branch not in result:
            result[branch] = {"total": 0.0, "payments": []}
        result[branch]["total"]    += p["amount"]
        result[branch]["payments"].append({
            "memo":   memo,
            "order":  origin,
            "amount": p["amount"],
        })

    return result
