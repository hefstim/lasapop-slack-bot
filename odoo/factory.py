"""LasaPop factory operations submitted from Slack (Lasa Food Odoo).

Owner decisions 2026-11-11:
- receipts, transfers and production are validated straight away; an admin can
  undo a receipt with a return;
- scrap and stock counts wait for an admin (FACTORY_ADMIN_IDS) to approve.
Lots are numbered by stock_picking_auto_create_lot on validation; the expiry
comes from the form, else from the product's expiration_time.
"""
from datetime import date, datetime, timedelta

from config import Config
from odoo.client import get_client

MAX_LINES = 8


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

    Returns the picking id. Raises if Odoo does not end up with the picking done.
    """
    client = get_client()
    ptype = _receipt_type()
    src = ptype["default_location_src_id"][0]
    dest = location_id or ptype["default_location_dest_id"][0]
    products = {p["id"]: p for p in client.search_read(
        "product.product", [("id", "in", [l["product_id"] for l in lines])], ["id", "uom_id", "tracking"])}
    picking_id = client.execute("stock.picking", "create", {
        "picking_type_id": ptype["id"],
        "partner_id": partner_id,
        "location_id": src,
        "location_dest_id": dest,
        "origin": f"Slack · {submitter}",
        "move_ids": [(0, 0, {
            "product_id": l["product_id"],
            "product_uom_qty": l["qty"],
            "product_uom": products[l["product_id"]]["uom_id"][0],
            "location_id": src,
            "location_dest_id": dest,
        }) for l in lines],
    })
    client.execute("stock.picking", "action_confirm", [picking_id])
    moves = client.search_read("stock.move", [("picking_id", "=", picking_id)],
                               ["id", "product_id", "product_uom_qty", "move_line_ids"], order="id")
    for move, line in zip(moves, lines):
        client.execute("stock.move", "write", [move["id"]], {"quantity": line["qty"]})
        if line.get("expiry"):
            mls = client.search_read("stock.move.line", [("move_id", "=", move["id"])], ["id"])
            exp = datetime.combine(line["expiry"], datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
            client.execute("stock.move.line", "write", [m["id"] for m in mls], {"expiration_date": exp})
    client.execute("stock.picking", "button_validate", [picking_id])

    state = client.search_read("stock.picking", [("id", "=", picking_id)], ["state"])[0]["state"]
    if state != "done":
        raise RuntimeError(f"receipt {picking_id} not validated (state {state}); check it in Odoo")
    for move, line in zip(moves, lines):
        if line.get("supplier_lot"):
            lot_ids = {m["lot_id"][0] for m in client.search_read(
                "stock.move.line", [("move_id", "=", move["id"]), ("lot_id", "!=", False)], ["lot_id"])}
            if lot_ids:
                client.execute("stock.lot", "write", list(lot_ids), {"ref": f"供应商批号 {line['supplier_lot']}"})
    body = f"Slack 收货，提交人 {submitter}" + (f"<br/>备注：{note}" if note else "")
    client.execute("stock.picking", "message_post", [picking_id], body=body, message_type="comment",
                   subtype_xmlid="mail.mt_note")
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
                   "expiry": (m["expiration_date"] or "")[:10]} for m in mls],
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
    client.execute("stock.picking", "action_assign", [ret_id])
    client.execute("stock.picking", "button_validate", [ret_id])
    ret = client.search_read("stock.picking", [("id", "=", ret_id)], ["name", "state"])[0]
    if ret["state"] != "done":
        client.execute("stock.picking", "action_cancel", [ret_id])
        raise RuntimeError(f"return {ret['name']} not validated (state {ret['state']}), cancelled it")
    client.execute("stock.picking", "message_post", [picking_id], body=f"已撤销（退回 {ret['name']}），操作人 {admin}",
                   message_type="comment", subtype_xmlid="mail.mt_note")
    return ret["name"]


def default_expiry(product_id: int) -> date:
    days = get_client().search_read("product.product", [("id", "=", product_id)],
                                    ["expiration_time"])[0]["expiration_time"] or 365
    return date.today() + timedelta(days=days)


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
        lot_exp = {l["id"]: (l["expiration_date"] or "")[:10]
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
    limit_date = (date.today() + timedelta(days=days)).isoformat()
    return [q for q in stock_quants(limit=1000) if q["expiry"] and q["expiry"] <= limit_date]


def internal_locations() -> list[dict]:
    return get_client().search_read("stock.location", [("usage", "=", "internal")], ["id", "complete_name"],
                                    order="complete_name")


def _note(model: str, rec_id: int, body: str):
    get_client().execute(model, "message_post", [rec_id], body=body, message_type="comment",
                         subtype_xmlid="mail.mt_note")


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
    client.execute("stock.picking", "button_validate", [picking_id])
    p = client.search_read("stock.picking", [("id", "=", picking_id)], ["name", "state"])[0]
    if p["state"] != "done":
        client.execute("stock.picking", "action_cancel", [picking_id])
        raise RuntimeError(f"调拨 {p['name']} 没有完成（{p['state']}），已取消")
    _note("stock.picking", picking_id, f"Slack 调拨，提交人 {submitter}" + (f"<br/>备注：{note}" if note else ""))
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
    _note("stock.scrap", scrap_id, f"Slack 报废申请，提交人 {submitter}" + (f"<br/>说明：{note}" if note else ""))
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
    _note("stock.scrap", scrap_id, f"已批准，批准人 {admin}")
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


def produce(bom_id: int, qty: float, submitter: str, note: str = "") -> int:
    """Create an MO from a BOM, consume components (FEFO reservation), produce and close it."""
    client = get_client()
    bom = client.search_read("mrp.bom", [("id", "=", bom_id)], ["product_tmpl_id", "product_id", "product_uom_id"])[0]
    product_id = bom["product_id"][0] if bom["product_id"] else client.search_read(
        "product.product", [("product_tmpl_id", "=", bom["product_tmpl_id"][0])], ["id"], limit=1)[0]["id"]
    mo_id = client.execute("mrp.production", "create", {
        "product_id": product_id, "bom_id": bom_id, "product_qty": qty,
        "product_uom_id": bom["product_uom_id"][0], "origin": f"Slack · {submitter}",
    })
    client.execute("mrp.production", "action_confirm", [mo_id])
    client.execute("mrp.production", "action_assign", [mo_id])
    short = []
    for m in client.search_read("stock.move", [("raw_material_production_id", "=", mo_id)],
                                ["product_id", "product_uom_qty", "quantity", "product_uom"]):
        if m["quantity"] + 1e-6 < m["product_uom_qty"]:
            short.append(f"{m['product_id'][1]} 需要 {m['product_uom_qty']:g}，库存只有 {m['quantity']:g} {m['product_uom'][1]}")
    if short:
        client.execute("mrp.production", "action_cancel", [mo_id])
        raise RuntimeError("原料不够，没有登记：\n" + "\n".join(short))
    mo = client.search_read("mrp.production", [("id", "=", mo_id)], ["product_tracking", "lot_producing_ids"])[0]
    if mo["product_tracking"] == "lot" and not mo["lot_producing_ids"]:
        client.execute("mrp.production", "action_generate_serial", [mo_id])
    client.execute("mrp.production", "write", [mo_id], {"qty_producing": qty})
    client.execute("mrp.production", "set_qty_producing", [mo_id])
    raw = client.search_read("stock.move", [("raw_material_production_id", "=", mo_id)], ["id"])
    client.execute("stock.move", "write", [m["id"] for m in raw], {"picked": True})
    client.execute("mrp.production", "button_mark_done", [mo_id], context={"skip_redirection": True,
                                                                           "skip_consumption": True})
    state = client.search_read("mrp.production", [("id", "=", mo_id)], ["state", "name"])[0]
    if state["state"] != "done":
        raise RuntimeError(f"生产单 {state['name']} 没有完成（{state['state']}），请在 Odoo 查看")
    _note("mrp.production", mo_id, f"Slack 生产登记，提交人 {submitter}" + (f"<br/>备注：{note}" if note else ""))
    return mo_id


def production_summary(mo_id: int) -> dict:
    client = get_client()
    mo = client.search_read("mrp.production", [("id", "=", mo_id)],
                            ["name", "product_id", "qty_produced", "product_uom_id", "lot_producing_ids"])[0]
    lots = client.search_read("stock.lot", [("id", "in", mo["lot_producing_ids"])], ["name", "expiration_date"])
    used = client.search_read("stock.move.line", [("move_id.raw_material_production_id", "=", mo_id)],
                              ["product_id", "lot_id", "quantity", "product_uom_id"], order="product_id")
    return {
        "name": mo["name"], "product": mo["product_id"][1], "qty": mo["qty_produced"], "uom": mo["product_uom_id"][1],
        "lots": [f"{l['name']}" + (f"（到期 {l['expiration_date'][:10]}）" if l["expiration_date"] else "") for l in lots],
        "used": [f"{u['product_id'][1]} {u['quantity']:g} {u['product_uom_id'][1]}" + (f" · {u['lot_id'][1]}" if u["lot_id"] else "")
                 for u in used],
    }
