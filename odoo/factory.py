"""LasaPop factory operations submitted from Slack (Lasa Food Odoo).

Owner decisions 2026-11-11:
- receipts, transfers and production are validated straight away; an admin can
  undo a receipt with a return;
- scrap and stock counts wait for an admin (FACTORY_ADMIN_IDS) to approve.
Lots are numbered by stock_picking_auto_create_lot on validation; the expiry
comes from the form, else from the product's expiration_time.
"""
import html
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from config import Config
from odoo.client import get_client

MAX_LINES = 8
TZ = ZoneInfo(Config.TIMEZONE or "Asia/Manila")
UTC = ZoneInfo("UTC")


def _local_date(value) -> str:
    """Odoo UTC datetime string -> local calendar date (expiry 2027-10-31 16:00 UTC is 11-01 in Manila)."""
    if not value:
        return ""
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).astimezone(TZ).date().isoformat()


def _utc_midnight(d: date) -> str:
    """Local midnight of `d` as an Odoo UTC datetime string (same convention as the opening-stock lots)."""
    return datetime.combine(d, time(), TZ).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> date:
    return datetime.now(TZ).date()


def _post(model: str, rec_id: int, text: str, note: str = ""):
    """Chatter note; `note` is user text, escaped."""
    body = html.escape(text) + (f"<br/>备注：{html.escape(note)}" if note else "")
    get_client().execute(model, "message_post", [rec_id], body=body, body_is_html=True,
                         message_type="comment", subtype_xmlid="mail.mt_note")


def _cancel_quietly(model: str, rec_id: int):
    try:
        get_client().execute(model, "action_cancel", [rec_id])
    except Exception:
        pass


def _receipt_type() -> dict:
    rows = get_client().search_read(
        "stock.picking.type", [("code", "=", "incoming")],
        ["id", "default_location_src_id", "default_location_dest_id"], limit=1, order="sequence, id")
    if not rows:
        raise RuntimeError("no Receipts operation type in Odoo")
    return rows[0]


def list_materials() -> list[dict]:
    """Purchasable stock products, for the form dropdown."""
    return get_client().search_read(
        "product.product",
        [("purchase_ok", "=", True), ("is_storable", "=", True)],
        ["id", "default_code", "name", "uom_id", "categ_id", "tracking"],
        order="default_code, name")


def list_vendors() -> list[dict]:
    """Vendors on product price lists first, then anyone flagged as a supplier."""
    client = get_client()
    ids = {r["partner_id"][0] for r in client.search_read("product.supplierinfo", [], ["partner_id"])}
    rows = client.search_read("res.partner", ["|", ("id", "in", list(ids)), ("supplier_rank", ">", 0)],
                              ["id", "name"], order="name")
    return rows


def create_receipt(partner_id: int, lines: list[dict], location_id: int | None, submitter: str,
                   note: str = "") -> int:
    """Create and validate a receipt. `lines`: product_id, qty, expiry (date|None), supplier_lot.

    One move per product (Odoo merges same-product moves anyway) and one move
    line per form line, so two supplier lots of the same material get two of
    our lots with their own expiry. Returns the picking id; on any failure the
    picking is cancelled and the error re-raised.
    """
    client = get_client()
    ptype = _receipt_type()
    src = ptype["default_location_src_id"][0]
    dest = location_id or ptype["default_location_dest_id"][0]
    products = {p["id"]: p for p in client.search_read(
        "product.product", [("id", "in", [l["product_id"] for l in lines])],
        ["id", "uom_id", "expiration_time", "use_expiration_date"])}
    totals: dict[int, float] = {}
    for l in lines:
        totals[l["product_id"]] = totals.get(l["product_id"], 0) + l["qty"]
    picking_id = client.execute("stock.picking", "create", {
        "picking_type_id": ptype["id"],
        "partner_id": partner_id,
        "location_id": src,
        "location_dest_id": dest,
        "origin": f"Slack · {submitter}",
        "move_ids": [(0, 0, {
            "product_id": pid,
            "product_uom_qty": qty,
            "product_uom": products[pid]["uom_id"][0],
            "location_id": src,
            "location_dest_id": dest,
        }) for pid, qty in totals.items()],
    })
    try:
        client.execute("stock.picking", "action_confirm", [picking_id])
        moves = {m["product_id"][0]: m for m in client.search_read(
            "stock.move", [("picking_id", "=", picking_id)], ["id", "product_id", "product_uom"])}
        old = client.search_read("stock.move.line", [("picking_id", "=", picking_id)], ["id"])
        if old:
            client.execute("stock.move.line", "unlink", [m["id"] for m in old])
        ml_ids = []
        for l in lines:
            p = products[l["product_id"]]
            vals = {
                "move_id": moves[l["product_id"]]["id"], "picking_id": picking_id, "product_id": l["product_id"],
                "product_uom_id": moves[l["product_id"]]["product_uom"][0], "quantity": l["qty"],
                "location_id": src, "location_dest_id": dest, "picked": True,
            }
            expiry = l.get("expiry") or (_today() + timedelta(days=p["expiration_time"] or 365)
                                          if p["use_expiration_date"] else None)
            if expiry:
                vals["expiration_date"] = _utc_midnight(expiry)
            ml_ids.append(client.execute("stock.move.line", "create", vals))
        client.execute("stock.picking", "button_validate", [picking_id])
        state = client.search_read("stock.picking", [("id", "=", picking_id)], ["state"])[0]["state"]
        if state != "done":
            raise RuntimeError(f"收货单没有完成（{state}）")
    except Exception:
        _cancel_quietly("stock.picking", picking_id)
        raise
    for ml_id, l in zip(ml_ids, lines):
        if l.get("supplier_lot"):
            lot = client.search_read("stock.move.line", [("id", "=", ml_id)], ["lot_id"])[0]["lot_id"]
            if lot:
                client.execute("stock.lot", "write", [lot[0]], {"ref": f"供应商批号 {l['supplier_lot']}"})
    _post("stock.picking", picking_id, f"Slack 收货，提交人 {submitter}", note)
    return picking_id


def receipt_summary(picking_id: int) -> dict:
    client = get_client()
    p = client.search_read("stock.picking", [("id", "=", picking_id)],
                           ["name", "partner_id", "state", "date_done", "location_dest_id"])[0]
    mls = client.search_read("stock.move.line", [("picking_id", "=", picking_id)],
                             ["product_id", "quantity", "product_uom_id", "lot_id", "expiration_date"], order="id")
    lot_refs = {}
    lot_ids = [m["lot_id"][0] for m in mls if m["lot_id"]]
    if lot_ids:
        lot_refs = {l["id"]: l["ref"] for l in client.search_read("stock.lot", [("id", "in", lot_ids)], ["ref"])}
    return {
        "id": picking_id, "name": p["name"], "partner": p["partner_id"][1] if p["partner_id"] else "", "state": p["state"],
        "location": p["location_dest_id"][1],
        "lines": [{"product": m["product_id"][1], "qty": m["quantity"], "uom": m["product_uom_id"][1],
                   "lot": m["lot_id"][1] if m["lot_id"] else "",
                   "lot_ref": (lot_refs.get(m["lot_id"][0]) or "") if m["lot_id"] else "",
                   "expiry": _local_date(m["expiration_date"])} for m in mls],
    }


def attach_file(res_id: int, name: str, content: bytes, model: str = "stock.picking"):
    import base64
    get_client().execute("ir.attachment", "create", {
        "name": name, "datas": base64.b64encode(content).decode(),
        "res_model": model, "res_id": res_id,
    })


def undo_receipt(picking_id: int, admin: str) -> str:
    """Return everything in a done receipt to the vendor and validate the return.

    Fails (Odoo error) if part of the stock was already used, because
    stock_no_negative refuses to take it below zero.
    """
    client = get_client()
    ctx = {"active_id": picking_id, "active_ids": [picking_id], "active_model": "stock.picking"}
    wiz = client.execute("stock.return.picking", "create", {"picking_id": picking_id}, context=ctx)
    lines = client.search_read("stock.return.picking.line", [("wizard_id", "=", wiz)], ["id", "move_id"])
    for l in lines:
        qty = client.search_read("stock.move", [("id", "=", l["move_id"][0])], ["quantity"])[0]["quantity"]
        client.execute("stock.return.picking.line", "write", [l["id"]], {"quantity": qty})
    action = client.execute("stock.return.picking", "action_create_returns", [wiz], context=ctx)
    ret_id = action["res_id"]
    try:
        client.execute("stock.picking", "action_assign", [ret_id])
        client.execute("stock.picking", "button_validate", [ret_id], context={"skip_expired": True})
        ret = client.search_read("stock.picking", [("id", "=", ret_id)], ["name", "state"])[0]
        if ret["state"] != "done":
            raise RuntimeError(f"退回单 {ret['name']} 没有完成（{ret['state']}），可能货已经移走或用掉")
    except Exception:
        _cancel_quietly("stock.picking", ret_id)
        raise
    _post("stock.picking", picking_id, f"已撤销（退回 {ret['name']}），操作人 {admin}")
    return ret["name"]


def list_receipt_locations() -> list[dict]:
    """Internal stock locations a delivery can be put away in (not finished goods or rejects)."""
    rows = get_client().search_read("stock.location", [("usage", "=", "internal")], ["id", "complete_name"],
                                    order="complete_name")
    return [r for r in rows if "Finished Goods" not in r["complete_name"] and "Rejected" not in r["complete_name"]]


def receipt_returned(picking_id: int) -> bool:
    return bool(get_client().execute("stock.picking", "search_count", [("return_id", "=", picking_id), ("state", "=", "done")]))


# ── Stock lookups ─────────────────────────────────────────────────────────────

def _short_loc(name: str) -> str:
    return name.split("/")[-1].strip()


def stock_quants(query: str = "", product_id: int | None = None, limit: int = 100) -> list[dict]:
    """On-hand stock per lot and location, earliest expiry first. `query` matches
    lot, product code or product name."""
    domain = [("location_id.usage", "=", "internal"), ("quantity", ">", 0)]
    if product_id:
        domain.append(("product_id", "=", product_id))
    if query:
        domain += ["|", "|", ("lot_id.name", "ilike", query), ("product_id.default_code", "ilike", query),
                   ("product_id.name", "ilike", query)]
    rows = get_client().search_read(
        "stock.quant", domain,
        ["id", "product_id", "lot_id", "location_id", "quantity", "reserved_quantity", "product_uom_id"],
        limit=limit, order="id")
    lot_exp = {}
    lot_ids = [r["lot_id"][0] for r in rows if r["lot_id"]]
    if lot_ids:
        lot_exp = {l["id"]: _local_date(l["expiration_date"])
                   for l in get_client().search_read("stock.lot", [("id", "in", lot_ids)], ["expiration_date"])}
    out = [{
        "quant_id": r["id"], "product_id": r["product_id"][0], "product": r["product_id"][1],
        "lot_id": r["lot_id"][0] if r["lot_id"] else False, "lot": r["lot_id"][1] if r["lot_id"] else "",
        "location_id": r["location_id"][0], "location": _short_loc(r["location_id"][1]),
        "qty": r["quantity"], "free": r["quantity"] - r["reserved_quantity"], "uom": r["product_uom_id"][1],
        "expiry": lot_exp.get(r["lot_id"][0], "") if r["lot_id"] else "",
    } for r in rows]
    return sorted(out, key=lambda q: (q["expiry"] or "9999", q["product"]))


def quant(quant_id: int) -> dict:
    rows = [q for q in stock_quants_by_ids([quant_id])]
    if not rows:
        raise RuntimeError("这个批次已经没有库存了")
    return rows[0]


def stock_quants_by_ids(ids: list[int]) -> list[dict]:
    rows = get_client().search_read("stock.quant", [("id", "in", ids)],
                                    ["id", "product_id", "lot_id", "location_id", "quantity", "reserved_quantity",
                                     "product_uom_id", "inventory_quantity", "inventory_diff_quantity"])
    return [{
        "quant_id": r["id"], "product_id": r["product_id"][0], "product": r["product_id"][1],
        "lot_id": r["lot_id"][0] if r["lot_id"] else False, "lot": r["lot_id"][1] if r["lot_id"] else "",
        "location_id": r["location_id"][0], "location": _short_loc(r["location_id"][1]),
        "qty": r["quantity"], "free": r["quantity"] - r["reserved_quantity"], "uom": r["product_uom_id"][1],
        "counted": r["inventory_quantity"], "diff": r["inventory_diff_quantity"],
    } for r in rows]


def expiring_lots(days: int) -> list[dict]:
    """On-hand lots that expire within `days` (or already expired)."""
    limit_date = (_today() + timedelta(days=days)).isoformat()
    return [q for q in stock_quants(limit=1000) if q["expiry"] and q["expiry"] <= limit_date]


def internal_locations() -> list[dict]:
    return get_client().search_read("stock.location", [("usage", "=", "internal")], ["id", "complete_name"],
                                    order="complete_name")


# ── Internal transfer (e.g. main store → batching room) ──────────────────────

def transfer(quant_id: int, qty: float, dest_location_id: int, submitter: str, note: str = "") -> int:
    client = get_client()
    q = quant(quant_id)
    if qty > q["free"] + 1e-6:
        raise RuntimeError(f"{q['lot']} 在 {q['location']} 只有 {q['free']:g} {q['uom']} 可以移动")
    if dest_location_id == q["location_id"]:
        raise RuntimeError("目的地和原来的位置相同")
    ptype = client.search_read("stock.picking.type", [("code", "=", "internal")], ["id"], limit=1, order="sequence, id")[0]
    picking_id = client.execute("stock.picking", "create", {
        "picking_type_id": ptype["id"], "location_id": q["location_id"], "location_dest_id": dest_location_id,
        "origin": f"Slack · {submitter}",
        "move_ids": [(0, 0, {"product_id": q["product_id"], "product_uom_qty": qty,
                             "location_id": q["location_id"], "location_dest_id": dest_location_id})],
    })
    try:
        client.execute("stock.picking", "action_confirm", [picking_id])
        move = client.search_read("stock.move", [("picking_id", "=", picking_id)], ["id", "product_uom"])[0]
        old = client.search_read("stock.move.line", [("move_id", "=", move["id"])], ["id"])
        if old:
            client.execute("stock.move.line", "unlink", [m["id"] for m in old])
        client.execute("stock.move.line", "create", {
            "move_id": move["id"], "picking_id": picking_id, "product_id": q["product_id"],
            "product_uom_id": move["product_uom"][0], "lot_id": q["lot_id"], "quantity": qty,
            "location_id": q["location_id"], "location_dest_id": dest_location_id, "picked": True,
        })
        # Moving an expired lot (e.g. to the rejects area) is allowed.
        client.execute("stock.picking", "button_validate", [picking_id], context={"skip_expired": True})
        p = client.search_read("stock.picking", [("id", "=", picking_id)], ["name", "state"])[0]
        if p["state"] != "done":
            raise RuntimeError(f"调拨 {p['name']} 没有完成（{p['state']}）")
    except Exception:
        _cancel_quietly("stock.picking", picking_id)
        raise
    _post("stock.picking", picking_id, f"Slack 调拨，提交人 {submitter}", note)
    return picking_id


def picking_name(picking_id: int) -> str:
    return get_client().search_read("stock.picking", [("id", "=", picking_id)], ["name"])[0]["name"]


# ── Scrap (needs approval) ───────────────────────────────────────────────────

def scrap_reasons() -> list[dict]:
    return get_client().search_read("scrap.reason.code", [], ["id", "name", "description"], order="id")


def create_scrap(quant_id: int, qty: float, reason_id: int, submitter: str, note: str = "") -> int:
    q = quant(quant_id)
    if qty > q["free"] + 1e-6:
        raise RuntimeError(f"{q['lot']} 在 {q['location']} 只有 {q['free']:g} {q['uom']}")
    client = get_client()
    uom = client.search_read("product.product", [("id", "=", q["product_id"])], ["uom_id"])[0]["uom_id"][0]
    scrap_id = client.execute("stock.scrap", "create", {
        "product_id": q["product_id"], "lot_id": q["lot_id"], "location_id": q["location_id"],
        "scrap_qty": qty, "product_uom_id": uom, "reason_code_id": reason_id,
        "origin": f"Slack · {submitter}",
    })
    _post("stock.scrap", scrap_id, f"Slack 报废申请，提交人 {submitter}", note)
    return scrap_id


def scrap_state(scrap_id: int) -> str:
    rows = get_client().search_read("stock.scrap", [("id", "=", scrap_id)], ["state"])
    return rows[0]["state"] if rows else "deleted"


def approve_scrap(scrap_id: int, admin: str) -> str:
    client = get_client()
    if scrap_state(scrap_id) != "draft":
        raise RuntimeError("这张报废单已经处理过了")
    res = client.execute("stock.scrap", "action_validate", [scrap_id])
    s = client.search_read("stock.scrap", [("id", "=", scrap_id)], ["name", "state"])[0]
    if s["state"] != "done":
        raise RuntimeError(f"库存不够，报废没有完成（{s['name']}）" if isinstance(res, dict) else f"{s['name']} 状态 {s['state']}")
    _post("stock.scrap", scrap_id, f"已批准，批准人 {admin}")
    return s["name"]


def reject_scrap(scrap_id: int):
    if scrap_state(scrap_id) != "draft":
        raise RuntimeError("这张报废单已经处理过了")
    get_client().execute("stock.scrap", "unlink", [scrap_id])


# ── Stock count (needs approval) ─────────────────────────────────────────────

def submit_count(counts: list[tuple[int, float]], submitter: str) -> list[dict]:
    """Record counted quantities on quants (not applied yet). Returns the lines with differences."""
    client = get_client()
    for quant_id, counted in counts:
        client.execute("stock.quant", "write", [quant_id], {"inventory_quantity": counted})
    return stock_quants_by_ids([c[0] for c in counts])


def apply_count(quant_ids: list[int], admin: str):
    client = get_client()
    rows = stock_quants_by_ids(quant_ids)
    pending = [r["quant_id"] for r in rows
               if client.search_read("stock.quant", [("id", "=", r["quant_id"])], ["inventory_quantity_set"])[0]["inventory_quantity_set"]]
    if not pending:
        raise RuntimeError("这次盘点已经处理过了")
    res = client.execute("stock.quant", "action_apply_inventory", pending)
    if isinstance(res, dict):
        raise RuntimeError("盘点之后库存又有变动，请重新盘点这些批次")
    return pending


def clear_count(quant_ids: list[int]):
    get_client().execute("stock.quant", "action_clear_inventory_quantity", quant_ids)


# ── Production (validated straight away) ─────────────────────────────────────

def list_boms() -> list[dict]:
    rows = get_client().search_read("mrp.bom", [("type", "=", "normal")],
                                    ["id", "product_tmpl_id", "product_id", "product_qty", "product_uom_id", "code"],
                                    order="product_tmpl_id")
    return rows


# Raw materials come from the Batching Room first, then Main Store; packaging from the
# Packaging area (manual 4.7 / 4.10). Matched on the English part of the location name.
PICK_ORDER = ("Batching Room", "Main Store", "Packaging")
NO_PICK = ("Rejected", "Finished Goods")
LOT_RE = r"^\d{8}-[A-Za-z0-9]+-\d{2}$"


def bom_components(bom_id: int) -> list[dict]:
    return get_client().search_read("mrp.bom.line", [("bom_id", "=", bom_id)],
                                    ["product_id", "product_qty", "product_uom_id"], order="sequence, id")


def suggest_lot(product_id: int) -> str:
    """Next finished-goods lot by the manual's rule: date-code-sequence, e.g. 20261020-F01-01."""
    code = get_client().search_read("product.product", [("id", "=", product_id)], ["default_code"])[0]["default_code"]
    prefix = f"{_today():%Y%m%d}-{code or product_id}-"
    names = [l["name"] for l in get_client().search_read(
        "stock.lot", [("product_id", "=", product_id), ("name", "=like", prefix + "%")], ["name"])]
    seqs = [int(n[len(prefix):]) for n in names if n[len(prefix):].isdigit()]
    return f"{prefix}{(max(seqs) + 1 if seqs else 1):02d}"


def bom_product(bom_id: int) -> int:
    client = get_client()
    bom = client.search_read("mrp.bom", [("id", "=", bom_id)], ["product_tmpl_id", "product_id"])[0]
    return bom["product_id"][0] if bom["product_id"] else client.search_read(
        "product.product", [("product_tmpl_id", "=", bom["product_tmpl_id"][0])], ["id"], limit=1)[0]["id"]


def _pick(product_id: int, need: float) -> tuple[list, float]:
    """Lots/locations to consume `need` from, by PICK_ORDER then earliest expiry; skips expired stock."""
    today = _today().isoformat()
    def rank(q):
        loc = next((i for i, name in enumerate(PICK_ORDER) if name in q["location"]), len(PICK_ORDER))
        return (loc, q["expiry"] or "9999")
    quants = [q for q in stock_quants(product_id=product_id, limit=500)
              if q["free"] > 0 and not any(n in q["location"] for n in NO_PICK)
              and not (q["expiry"] and q["expiry"] <= today)]
    picks, left = [], need
    for q in sorted(quants, key=rank):
        if left <= 1e-9:
            break
        take = min(q["free"], left)
        picks.append((q, take))
        left -= take
    return picks, left


def produce(bom_id: int, qty: float, submitter: str, note: str = "", lot_name: str = "",
            actual: dict[int, float] | None = None) -> int:
    """Create an MO from a BOM and close it at once.

    `lot_name`: finished-goods lot (manual rule date-code-seq); suggested when empty.
    `actual`: product_id -> quantity really used, for components that differ from the recipe.
    Components are taken by PICK_ORDER; on any failure the MO is cancelled.
    """
    import re
    client = get_client()
    actual = actual or {}
    bom = client.search_read("mrp.bom", [("id", "=", bom_id)], ["product_uom_id"])[0]
    product_id = bom_product(bom_id)
    tracking = client.search_read("product.product", [("id", "=", product_id)], ["tracking"])[0]["tracking"]
    if tracking == "lot":
        lot_name = (lot_name or suggest_lot(product_id)).strip()
        if not re.match(LOT_RE, lot_name):
            raise RuntimeError(f"成品批号 {lot_name} 格式不对，应为 日期-成品代码-序号，例如 20261020-F01-01")
        if client.execute("stock.lot", "search_count", [("product_id", "=", product_id), ("name", "=", lot_name)]):
            raise RuntimeError(f"成品批号 {lot_name} 已经用过了")
    mo_id = client.execute("mrp.production", "create", {
        "product_id": product_id, "bom_id": bom_id, "product_qty": qty,
        "product_uom_id": bom["product_uom_id"][0], "origin": f"Slack · {submitter}",
    })
    try:
        client.execute("mrp.production", "action_confirm", [mo_id])
        if tracking == "lot":
            lot_id = client.execute("stock.lot", "create", {"name": lot_name, "product_id": product_id})
            client.execute("mrp.production", "write", [mo_id], {"lot_producing_ids": [(6, 0, [lot_id])]})
        client.execute("mrp.production", "write", [mo_id], {"qty_producing": qty})
        client.execute("mrp.production", "set_qty_producing", [mo_id])
        raws = client.search_read("stock.move", [("raw_material_production_id", "=", mo_id)],
                                  ["id", "product_id", "product_uom_qty", "product_uom", "location_dest_id"])
        uoms = {p["id"]: p["uom_id"][0] for p in client.search_read(
            "product.product", [("id", "in", [m["product_id"][0] for m in raws])], ["uom_id"])}
        short, plan = [], []
        for m in raws:
            pid = m["product_id"][0]
            if m["product_uom"][0] != uoms[pid]:
                raise RuntimeError(f"{m['product_id'][1]} 的配方单位和库存单位不同，请在 Odoo 处理")
            need = actual.get(pid, m["product_uom_qty"])
            picks, left = _pick(pid, need)
            if left > 1e-6:
                have = need - left
                short.append(f"{m['product_id'][1]} 需要 {need:g}，可用库存只有 {have:g} {m['product_uom'][1]}")
            plan.append((m, picks))
        if short:
            raise RuntimeError("原料不够（已过期、不良品区和成品仓的货不算），没有登记：\n" + "\n".join(short))
        for m, picks in plan:
            old = client.search_read("stock.move.line", [("move_id", "=", m["id"])], ["id"])
            if old:
                client.execute("stock.move.line", "unlink", [o["id"] for o in old])
            for q, take in picks:
                client.execute("stock.move.line", "create", {
                    "move_id": m["id"], "product_id": q["product_id"], "product_uom_id": m["product_uom"][0],
                    "lot_id": q["lot_id"], "quantity": take, "location_id": q["location_id"],
                    "location_dest_id": m["location_dest_id"][0], "picked": True,
                })
        client.execute("stock.move", "write", [m["id"] for m, _ in plan], {"picked": True})
        client.execute("mrp.production", "button_mark_done", [mo_id], context={"skip_redirection": True,
                                                                               "skip_consumption": True})
        state = client.search_read("mrp.production", [("id", "=", mo_id)], ["state", "name"])[0]
        if state["state"] != "done":
            raise RuntimeError(f"生产单 {state['name']} 没有完成（{state['state']}）")
    except Exception:
        _cancel_quietly("mrp.production", mo_id)
        raise
    _post("mrp.production", mo_id, f"Slack 生产登记，提交人 {submitter}", note)
    return mo_id


def production_summary(mo_id: int) -> dict:
    client = get_client()
    mo = client.search_read("mrp.production", [("id", "=", mo_id)],
                            ["name", "product_id", "qty_produced", "product_uom_id", "lot_producing_ids"])[0]
    lots = client.search_read("stock.lot", [("id", "in", mo["lot_producing_ids"])], ["name", "expiration_date"])
    used = client.search_read("stock.move.line", [("move_id.raw_material_production_id", "=", mo_id)],
                              ["product_id", "lot_id", "quantity", "product_uom_id", "location_id"], order="product_id")
    return {
        "name": mo["name"], "product": mo["product_id"][1], "qty": mo["qty_produced"], "uom": mo["product_uom_id"][1],
        "lots": [f"{l['name']}" + (f"（到期 {_local_date(l['expiration_date'])}）" if l["expiration_date"] else "") for l in lots],
        "used": [f"{u['product_id'][1]} {u['quantity']:g} {u['product_uom_id'][1]}" + (f" · {u['lot_id'][1]}" if u["lot_id"] else "")
                 + f" · {_short_loc(u['location_id'][1])}" for u in used],
    }
