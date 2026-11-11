"""LasaPop factory forms in Slack.

Staff (FACTORY_USER_IDS) open forms from the menu messages in the warehouse
and production channels. Receipts, transfers and production are done in Odoo
at once; scrap and stock counts post a card that only FACTORY_ADMIN_IDS can
approve. Registered from slack/app.py only when FACTORY_ENABLED is on (LasaPop).
"""
import json
import logging
import threading
from datetime import date

import requests
from slack_bolt import App

from config import Config
from odoo import factory

logger = logging.getLogger(__name__)

_undo_lock = threading.Lock()
_undoing: set[int] = set()


def _btn(action_id: str, text: str, style: str = "") -> dict:
    b = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}
    if style:
        b["style"] = style
    return b


def menu_blocks(kind: str = "warehouse") -> list:
    if kind == "production":
        buttons = [_btn("factory_produce_open", "🏭 生产入库 / Production", "primary"),
                   _btn("factory_query_open", "🔍 查库存 / Stock")]
        title = "*🏭 生产登记 / Production forms*"
    else:
        buttons = [_btn("factory_receipt_open", "📦 原料收货 / Receive", "primary"),
                   _btn("factory_transfer_open", "🔀 调拨领料 / Move"),
                   _btn("factory_scrap_open", "🗑 报废 / Scrap"),
                   _btn("factory_count_open", "📋 盘点 / Count"),
                   _btn("factory_query_open", "🔍 查库存 / Stock")]
        title = "*🏭 仓库登记 / Warehouse forms*"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{title}\n点按钮打开表单 · Tap a button to open a form"}},
        {"type": "actions", "elements": buttons},
    ]


def _name(client, user_id: str) -> str:
    try:
        u = client.users_info(user=user_id)["user"]
        return u.get("real_name") or u.get("name") or user_id
    except Exception:
        return user_id


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value}


def _receipt_view(rows: int, vendors: list, materials: list, locations: list) -> dict:
    groups: dict[str, list] = {}
    for m in materials:
        label = f"[{m['default_code']}] {m['name']} ({m['uom_id'][1]})" if m["default_code"] else m["name"]
        groups.setdefault(m["categ_id"][1].split(" / ")[-1], []).append(_option(label, str(m["id"])))
    option_groups = [{"label": {"type": "plain_text", "text": g[:75]}, "options": opts[:100]}
                     for g, opts in groups.items()]
    vendor_opts = [_option(v["name"], str(v["id"])) for v in vendors][:100]
    default_vendor = next((o for o in vendor_opts if o["text"]["text"] == "FLG"), None)
    loc_opts = [_option(l["complete_name"].replace("LP/Stock/", ""), str(l["id"])) for l in locations][:100]
    default_loc = next((o for o in loc_opts if "Main Store" in o["text"]["text"]), loc_opts[0] if loc_opts else None)

    vendor_el = {"type": "static_select", "action_id": "v", "options": vendor_opts,
                 "placeholder": {"type": "plain_text", "text": "选择供应商"}}
    if default_vendor:
        vendor_el["initial_option"] = default_vendor
    loc_el = {"type": "static_select", "action_id": "v", "options": loc_opts}
    if default_loc:
        loc_el["initial_option"] = default_loc

    blocks = [
        {"type": "input", "block_id": "vendor", "label": {"type": "plain_text", "text": "供应商 / Supplier"},
         "element": vendor_el},
        {"type": "input", "block_id": "location", "label": {"type": "plain_text", "text": "放到哪里 / Put in"},
         "element": loc_el},
    ]
    for i in range(rows):
        blocks += [
            {"type": "header", "text": {"type": "plain_text", "text": f"原料 {i + 1} / Item {i + 1}"}},
            {"type": "input", "block_id": f"prod_{i}", "label": {"type": "plain_text", "text": "原料 / Material"},
             "element": {"type": "static_select", "action_id": "v", "option_groups": option_groups,
                         "placeholder": {"type": "plain_text", "text": "选择原料"}}},
            {"type": "input", "block_id": f"qty_{i}", "label": {"type": "plain_text", "text": "数量 / Quantity"},
             "element": {"type": "number_input", "action_id": "v", "is_decimal_allowed": True, "min_value": "0.001"}},
            {"type": "input", "block_id": f"exp_{i}", "optional": True,
             "label": {"type": "plain_text", "text": "到期日 / Expiry（不填按保质期自动算）"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": f"lot_{i}", "optional": True,
             "label": {"type": "plain_text", "text": "供应商批号 / Supplier lot no."},
             "element": {"type": "plain_text_input", "action_id": "v"}},
        ]
    row_buttons = []
    if rows < factory.MAX_LINES:
        row_buttons.append({"type": "button", "action_id": "factory_receipt_add_row",
                            "text": {"type": "plain_text", "text": "➕ 加一行 / Add item"}})
    if rows > 1:
        row_buttons.append({"type": "button", "action_id": "factory_receipt_remove_row",
                            "text": {"type": "plain_text", "text": "➖ 删最后一行 / Remove last"}})
    if row_buttons:
        blocks.append({"type": "actions", "block_id": "rows", "elements": row_buttons})
    blocks += [
        {"type": "input", "block_id": "photo", "optional": True,
         "label": {"type": "plain_text", "text": "送货单 / 货物照片 · Photos"},
         "element": {"type": "file_input", "action_id": "v", "max_files": 5,
                     "filetypes": ["png", "jpg", "jpeg", "heic", "pdf"]}},
        {"type": "input", "block_id": "note", "optional": True, "label": {"type": "plain_text", "text": "备注 / Note"},
         "element": {"type": "plain_text_input", "action_id": "v", "multiline": True}},
    ]
    return {
        "type": "modal", "callback_id": "factory_receipt_modal",
        "title": {"type": "plain_text", "text": "原料收货"},
        "submit": {"type": "plain_text", "text": "确认入库"},
        "close": {"type": "plain_text", "text": "取消"},
        "private_metadata": json.dumps({"rows": rows}),
        "blocks": blocks,
    }


def _form_data() -> tuple:
    return factory.list_vendors(), factory.list_materials(), factory.list_receipt_locations()


def _card_blocks(s: dict, submitter: str, photos: int) -> list:
    lines = []
    for l in s["lines"]:
        extra = f" · {l['lot_ref']}" if l["lot_ref"] else ""
        lines.append(f"• {l['product']} — *{l['qty']:g} {l['uom']}* · 批号 `{l['lot']}` · 到期 {l['expiry']}{extra}")
    ctx = f"提交人 {submitter} · 库位 {s['location'].replace('LP/Stock/', '')}"
    if photos:
        ctx += f" · 照片 {photos} 张已存到 Odoo"
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"📦 *原料收货 {s['name']}* ✅ 已入库\n供应商：{s['partner']}\n" + "\n".join(lines)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]},
        {"type": "actions", "block_id": "receipt_actions", "elements": [
            {"type": "button", "action_id": "factory_receipt_undo", "style": "danger", "value": str(s["id"]),
             "text": {"type": "plain_text", "text": "撤销（仅管理员）"},
             "confirm": {"title": {"type": "plain_text", "text": "撤销这次收货？"},
                         "text": {"type": "mrkdwn", "text": f"{s['name']} 的货会全部退回供应商，库存扣回。"},
                         "confirm": {"type": "plain_text", "text": "撤销"},
                         "deny": {"type": "plain_text", "text": "取消"}}},
        ]},
    ]


def _submit_receipt(client, user_id: str, partner_id: int, location_id: int, lines: list, files: list, note: str):
    submitter = _name(client, user_id)
    try:
        picking_id = factory.create_receipt(partner_id, lines, location_id, submitter, note)
    except Exception as e:
        logger.error("factory receipt failed: %s", e)
        client.chat_postMessage(channel=user_id, text=f"❌ 收货没有登记成功，请重试或联系管理员。\n{e}")
        client.chat_postMessage(channel=Config.SLACK_CHANNEL_MANAGEMENT,
                                text=f"❌ {submitter} 的原料收货登记失败：{e}")
        return
    photos = 0
    for f in files:
        try:
            url, name = f.get("url_private"), f.get("name", "photo.jpg")
            if not url and f.get("id"):
                info = client.files_info(file=f["id"])["file"]
                url, name = info.get("url_private"), info.get("name", name)
            r = requests.get(url, headers={"Authorization": f"Bearer {Config.SLACK_BOT_TOKEN}"}, timeout=30)
            r.raise_for_status()
            factory.attach_file(picking_id, name, r.content)
            photos += 1
        except Exception as e:
            logger.error("factory receipt %s: photo attach failed: %s", picking_id, e)
    s = factory.receipt_summary(picking_id)
    s["id"] = picking_id
    client.chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE, blocks=_card_blocks(s, submitter, photos),
                            text=f"原料收货 {s['name']} 已入库")


def register_factory_handlers(app: App):

    @app.action("factory_receipt_open")
    def open_receipt(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if user_id not in Config.FACTORY_USER_IDS:
            client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id,
                                      text="⛔ 你还没有开通工厂登记权限，请联系管理员。")
            return
        client.views_open(trigger_id=body["trigger_id"], view=_receipt_view(1, *_form_data()))

    def _resize(body, client, delta):
        view = body["view"]
        rows = json.loads(view["private_metadata"] or "{}").get("rows", 1) + delta
        rows = max(1, min(factory.MAX_LINES, rows))
        client.views_update(view_id=view["id"], hash=view["hash"], view=_receipt_view(rows, *_form_data()))

    @app.action("factory_receipt_add_row")
    def add_row(ack, body, client):
        ack()
        _resize(body, client, 1)

    @app.action("factory_receipt_remove_row")
    def remove_row(ack, body, client):
        ack()
        _resize(body, client, -1)

    @app.view("factory_receipt_modal")
    def submit_receipt(ack, body, client, view):
        user_id = body["user"]["id"]
        if user_id not in Config.FACTORY_USER_IDS:
            ack(response_action="errors", errors={"vendor": "你没有工厂登记权限"})
            return
        v = view["state"]["values"]
        rows = json.loads(view["private_metadata"] or "{}").get("rows", 1)
        errors, lines = {}, []
        for i in range(rows):
            product = v[f"prod_{i}"]["v"].get("selected_option")
            qty_raw = v[f"qty_{i}"]["v"].get("value")
            exp_raw = v[f"exp_{i}"]["v"].get("selected_date")
            lot = (v[f"lot_{i}"]["v"].get("value") or "").strip()
            try:
                qty = float(qty_raw)
                if qty <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors[f"qty_{i}"] = "请输入大于 0 的数量"
                continue
            expiry = date.fromisoformat(exp_raw) if exp_raw else None
            if expiry and expiry <= factory._today():
                errors[f"exp_{i}"] = "到期日必须晚于今天"
                continue
            lines.append({"product_id": int(product["value"]), "qty": qty, "expiry": expiry, "supplier_lot": lot})
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        partner_id = int(v["vendor"]["v"]["selected_option"]["value"])
        location_id = int(v["location"]["v"]["selected_option"]["value"])
        files = v.get("photo", {}).get("v", {}).get("files") or []
        note = (v.get("note", {}).get("v", {}).get("value") or "").strip()
        threading.Thread(target=_submit_receipt,
                         args=(client, user_id, partner_id, location_id, lines, files, note), daemon=True).start()

    @app.action("factory_receipt_undo")
    def undo(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        if user_id not in Config.FACTORY_ADMIN_IDS:
            client.chat_postEphemeral(channel=channel, user=user_id, text="⛔ 只有管理员可以撤销收货。")
            return
        picking_id = int(body["actions"][0]["value"])
        with _undo_lock:
            if picking_id in _undoing:
                return
            _undoing.add(picking_id)
        message = body["message"]

        def _run():
            admin = _name(client, user_id)
            try:
                if factory.receipt_returned(picking_id):
                    raise RuntimeError("这张收货单已经撤销过了")
                ret = factory.undo_receipt(picking_id, admin)
            except Exception as e:
                logger.error("undo receipt %s failed: %s", picking_id, e)
                client.chat_postEphemeral(channel=channel, user=user_id,
                                          text=f"❌ 撤销失败：{e}\n（如果这批原料已经用掉一部分，系统不允许撤销，请在 Odoo 处理。）")
                return
            finally:
                with _undo_lock:
                    _undoing.discard(picking_id)
            blocks = [b for b in message["blocks"] if b.get("block_id") != "receipt_actions"]
            blocks[0]["text"]["text"] = blocks[0]["text"]["text"].replace("✅ 已入库", "↩️ 已撤销")
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                                                            "text": f"↩️ {admin} 已撤销，退回单 {ret}"}]})
            client.chat_update(channel=channel, ts=message["ts"], blocks=blocks, text=f"收货已撤销（{ret}）")

        threading.Thread(target=_run, daemon=True).start()

    register_stock_handlers(app)


# ── Transfer / scrap / count / production / stock query ─────────────────────

def _denied(client, body) -> bool:
    user_id = body["user"]["id"]
    if user_id in Config.FACTORY_USER_IDS:
        return False
    channel = (body.get("channel") or {}).get("id")
    if channel:
        client.chat_postEphemeral(channel=channel, user=user_id, text="⛔ 你还没有开通工厂登记权限，请联系管理员。")
    return True


def _admin_only(client, body) -> bool:
    """True (and tells the user) when the clicker is not a factory admin."""
    if body["user"]["id"] in Config.FACTORY_ADMIN_IDS:
        return False
    client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text="⛔ 只有管理员可以批准或驳回。")
    return True


def _claim(key) -> bool:
    with _undo_lock:
        if key in _undoing:
            return False
        _undoing.add(key)
        return True


def _release(key):
    with _undo_lock:
        _undoing.discard(key)


def _meta(view: dict) -> dict:
    return json.loads(view.get("private_metadata") or "{}")


def _quant_label(q: dict) -> str:
    label = f"{q['lot'] or '无批号'} · {q['location']} · {q['qty']:g} {q['uom']}"
    return label + (f" · 到期 {q['expiry']}" if q["expiry"] else "")


def _product_select(quants: list, action_id: str, selected: int | None) -> dict:
    names: dict[int, str] = {}
    for q in quants:
        names.setdefault(q["product_id"], q["product"])
    opts = [_option(n, str(pid)) for pid, n in sorted(names.items(), key=lambda x: x[1])][:100]
    el = {"type": "static_select", "action_id": action_id, "options": opts or [_option("（没有库存）", "0")],
          "placeholder": {"type": "plain_text", "text": "选择原料 / 产品"}}
    init = next((o for o in opts if o["value"] == str(selected)), None)
    if init:
        el["initial_option"] = init
    return el


def _input(block_id: str, label: str, element: dict, optional: bool = False, **extra) -> dict:
    b = {"type": "input", "block_id": block_id, "label": {"type": "plain_text", "text": label}, "element": element}
    if optional:
        b["optional"] = True
    b.update(extra)
    return b


def _qty_el() -> dict:
    return {"type": "number_input", "action_id": "v", "is_decimal_allowed": True, "min_value": "0.001"}


def _note_block() -> dict:
    return _input("note", "备注 / Note", {"type": "plain_text_input", "action_id": "v", "multiline": True}, optional=True)


def _photo_block(label: str = "照片 / Photos") -> dict:
    return _input("photo", label, {"type": "file_input", "action_id": "v", "max_files": 5,
                                   "filetypes": ["png", "jpg", "jpeg", "heic", "pdf"]}, optional=True)


def _modal(callback_id: str, title: str, submit: str | None, blocks: list, meta: dict) -> dict:
    v = {"type": "modal", "callback_id": callback_id, "title": {"type": "plain_text", "text": title},
         "close": {"type": "plain_text", "text": "取消"}, "private_metadata": json.dumps(meta), "blocks": blocks}
    if submit:
        v["submit"] = {"type": "plain_text", "text": submit}
    return v


def _lot_form_view(form: str, meta: dict) -> dict:
    """Transfer and scrap: pick a product, then one of its lots (the list refreshes on product change)."""
    meta = {**meta, "form": form}
    quants = factory.stock_quants(limit=1000)
    blocks = [_input("product", "原料 / 产品 · Item", _product_select(quants, "factory_pick_product", meta.get("product_id")),
                     dispatch_action=True)]
    if meta.get("product_id"):
        opts = [_option(_quant_label(q), str(q["quant_id"])) for q in quants if q["product_id"] == meta["product_id"]][:100]
        if opts:
            blocks.append(_input("quant", "批次 · 位置 · 数量 / Lot", {"type": "static_select", "action_id": "v", "options": opts,
                                                                   "placeholder": {"type": "plain_text", "text": "选择批次"}}))
        else:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "这个原料现在没有库存。"}]})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "先选原料，再选批次 · Pick the item first"}]})
    blocks.append(_input("qty", "数量 / Quantity", _qty_el()))
    if form == "transfer":
        locs = [_option(l["complete_name"].replace("LP/Stock/", ""), str(l["id"])) for l in factory.internal_locations()][:100]
        blocks.append(_input("dest", "移到 / Move to", {"type": "static_select", "action_id": "v", "options": locs}))
        blocks.append(_note_block())
        return _modal("factory_transfer_modal", "调拨领料", "确认调拨", blocks, meta)
    reasons = [_option(r["description"] or r["name"], str(r["id"])) for r in factory.scrap_reasons()]
    blocks.append(_input("reason", "原因 / Reason", {"type": "static_select", "action_id": "v", "options": reasons}))
    blocks.append(_photo_block())
    blocks.append(_note_block())
    return _modal("factory_scrap_modal", "报废申请", "提交审批", blocks, meta)


def _count_view(meta: dict) -> dict:
    locs = [_option(l["complete_name"].replace("LP/Stock/", ""), str(l["id"])) for l in factory.internal_locations()][:100]
    loc_el = {"type": "static_select", "action_id": "factory_pick_location", "options": locs,
              "placeholder": {"type": "plain_text", "text": "选择要盘点的位置"}}
    init = next((o for o in locs if o["value"] == str(meta.get("location_id"))), None)
    if init:
        loc_el["initial_option"] = init
    blocks = [_input("location", "盘点位置 / Location", loc_el, dispatch_action=True)]
    if meta.get("location_id"):
        quants = [q for q in factory.stock_quants(limit=1000) if q["location_id"] == meta["location_id"]][:90]
        if not quants:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "这个位置没有库存记录。"}]})
        else:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                "逐项填写*实际数到的数量*（只填数过的，不填的不算）· Enter what you actually counted"}]})
        for q in quants:
            blocks.append(_input(f"c_{q['quant_id']}", f"{q['lot'] or '无批号'} · {q['product']} ({q['uom']})"[:150],
                                 {"type": "number_input", "action_id": "v", "is_decimal_allowed": True, "min_value": "0"},
                                 optional=True))
    return _modal("factory_count_modal", "盘点", "提交审批" if meta.get("location_id") else None, blocks, meta)


def _produce_view() -> dict:
    boms = factory.list_boms()
    if not boms:
        return _modal("factory_produce_modal", "生产入库", None, [{"type": "section", "text": {"type": "mrkdwn", "text":
            "还没有配方（BOM）。请管理员先在 Odoo 录入配方，再用这个表单。"}}], {})
    opts = [_option(f"{b['product_tmpl_id'][1]}（配方每 {b['product_qty']:g} {b['product_uom_id'][1]}）"
                    + (f" {b['code']}" if b["code"] else ""), str(b["id"])) for b in boms][:100]
    blocks = [
        _input("bom", "产品 / Product", {"type": "static_select", "action_id": "v", "options": opts}),
        _input("qty", "生产数量（按配方单位）/ Quantity made", _qty_el()),
        _note_block(),
    ]
    return _modal("factory_produce_modal", "生产入库", "确认入库", blocks, {})


def _query_view() -> dict:
    quants = factory.stock_quants(limit=1000)
    return _modal("factory_query_modal", "查库存", "查询",
                  [_input("product", "原料 / 产品 · Item", _product_select(quants, "v", None))], {})


def _download(client, files: list) -> list[tuple[str, bytes]]:
    out = []
    for f in files:
        try:
            url, name = f.get("url_private"), f.get("name", "photo.jpg")
            if not url and f.get("id"):
                info = client.files_info(file=f["id"])["file"]
                url, name = info.get("url_private"), info.get("name", name)
            r = requests.get(url, headers={"Authorization": f"Bearer {Config.SLACK_BOT_TOKEN}"}, timeout=30)
            r.raise_for_status()
            out.append((name, r.content))
        except Exception as e:
            logger.error("factory: photo download failed: %s", e)
    return out


def _fail(client, user_id: str, who: str, what: str, err: Exception):
    logger.error("factory %s failed: %s", what, err)
    client.chat_postMessage(channel=user_id, text=f"❌ {what}没有登记成功：\n{err}")
    client.chat_postMessage(channel=Config.SLACK_CHANNEL_MANAGEMENT, text=f"❌ {who} 的{what}失败：{err}")


def _qty_value(v: dict, block: str, errors: dict) -> float | None:
    try:
        q = float(v[block]["v"].get("value"))
        if q <= 0:
            raise ValueError
        return q
    except (TypeError, ValueError, KeyError):
        errors[block] = "请输入大于 0 的数量"
        return None


def _selected(v: dict, block: str) -> int | None:
    opt = (v.get(block) or {}).get("v", {}).get("selected_option")
    return int(opt["value"]) if opt else None


def _approval_buttons(prefix: str, value: str) -> dict:
    return {"type": "actions", "block_id": "approval", "elements": [
        {"type": "button", "action_id": f"{prefix}_approve", "style": "primary", "value": value,
         "text": {"type": "plain_text", "text": "✅ 批准（仅管理员）"}},
        {"type": "button", "action_id": f"{prefix}_reject", "style": "danger", "value": value,
         "text": {"type": "plain_text", "text": "❌ 驳回"}},
    ]}


def _close_card(client, body, old: str, new: str, footer: str):
    message = body["message"]
    blocks = [b for b in message["blocks"] if b.get("block_id") != "approval"]
    blocks[0]["text"]["text"] = blocks[0]["text"]["text"].replace(old, new)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    client.chat_update(channel=body["channel"]["id"], ts=message["ts"], blocks=blocks, text=footer)


def register_stock_handlers(app: App):

    def _opener(action_id: str, build):
        @app.action(action_id)
        def _open(ack, body, client):
            ack()
            if _denied(client, body):
                return
            client.views_open(trigger_id=body["trigger_id"], view=build())

    _opener("factory_transfer_open", lambda: _lot_form_view("transfer", {}))
    _opener("factory_scrap_open", lambda: _lot_form_view("scrap", {}))
    _opener("factory_count_open", lambda: _count_view({}))
    _opener("factory_produce_open", _produce_view)

    @app.action("factory_query_open")
    def open_query(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view=_query_view())

    @app.action("factory_pick_product")
    def pick_product(ack, body, client):
        ack()
        view = body["view"]
        meta = _meta(view)
        meta["product_id"] = int(body["actions"][0]["selected_option"]["value"])
        client.views_update(view_id=view["id"], hash=view["hash"], view=_lot_form_view(meta["form"], meta))

    @app.action("factory_pick_location")
    def pick_location(ack, body, client):
        ack()
        view = body["view"]
        meta = _meta(view)
        meta["location_id"] = int(body["actions"][0]["selected_option"]["value"])
        client.views_update(view_id=view["id"], hash=view["hash"], view=_count_view(meta))

    # Transfer — done at once
    @app.view("factory_transfer_modal")
    def submit_transfer(ack, body, client, view):
        v, errors = view["state"]["values"], {}
        quant_id = _selected(v, "quant")
        if not quant_id:
            errors["product"] = "请先选原料，再选批次"
        qty = _qty_value(v, "qty", errors)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        user_id = body["user"]["id"]
        dest = _selected(v, "dest")
        note = (v.get("note", {}).get("v", {}).get("value") or "").strip()

        def _run():
            who = _name(client, user_id)
            try:
                q = factory.quant(quant_id)
                pid = factory.transfer(quant_id, qty, dest, who, note)
                dest_name = next(l["complete_name"] for l in factory.internal_locations() if l["id"] == dest)
                text = (f"🔀 *调拨 {factory.picking_name(pid)}* ✅ 已完成\n{q['product']} — *{qty:g} {q['uom']}* · 批号 `{q['lot']}`\n"
                        f"{q['location']} → {factory._short_loc(dest_name)}" + (f"\n备注：{note}" if note else ""))
                client.chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE, text=text, blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": f"提交人 {who}"}]}])
            except Exception as e:
                _fail(client, user_id, who, "调拨", e)

        threading.Thread(target=_run, daemon=True).start()

    # Scrap — waits for an admin
    @app.view("factory_scrap_modal")
    def submit_scrap(ack, body, client, view):
        v, errors = view["state"]["values"], {}
        quant_id = _selected(v, "quant")
        if not quant_id:
            errors["product"] = "请先选原料，再选批次"
        qty = _qty_value(v, "qty", errors)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        user_id = body["user"]["id"]
        reason_id = _selected(v, "reason")
        files = v.get("photo", {}).get("v", {}).get("files") or []
        note = (v.get("note", {}).get("v", {}).get("value") or "").strip()

        def _run():
            who = _name(client, user_id)
            try:
                q = factory.quant(quant_id)
                scrap_id = factory.create_scrap(quant_id, qty, reason_id, who, note)
                photos = _download(client, files)
                for name, content in photos:
                    factory.attach_file(scrap_id, name, content, model="stock.scrap")
                reason = next((r["description"] or r["name"]) for r in factory.scrap_reasons() if r["id"] == reason_id)
                text = (f"🗑 *报废申请* ⏳ 待批准\n{q['product']} — *{qty:g} {q['uom']}* · 批号 `{q['lot']}` · {q['location']}\n"
                        f"原因：{reason}" + (f"\n说明：{note}" if note else ""))
                ctx = f"提交人 {who}" + (f" · 照片 {len(photos)} 张已存到 Odoo" if photos else "")
                client.chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE, text="报废申请待批准", blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]},
                    _approval_buttons("factory_scrap", str(scrap_id))])
            except Exception as e:
                _fail(client, user_id, who, "报废申请", e)

        threading.Thread(target=_run, daemon=True).start()

    def _decide(body, client, key, work, old, new_ok):
        if _admin_only(client, body) or not _claim(key):
            return
        admin = _name(client, body["user"]["id"])

        def _run():
            try:
                footer = work(admin)
                _close_card(client, body, old, new_ok, footer)
            except Exception as e:
                logger.error("factory decision %s failed: %s", key, e)
                client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text=f"❌ 操作失败：{e}")
            finally:
                _release(key)

        threading.Thread(target=_run, daemon=True).start()

    @app.action("factory_scrap_approve")
    def scrap_approve(ack, body, client):
        ack()
        sid = int(body["actions"][0]["value"])
        _decide(body, client, ("scrap", sid),
                lambda admin: f"✅ {admin} 已批准，报废单 {factory.approve_scrap(sid, admin)}，库存已扣除",
                "⏳ 待批准", "✅ 已批准")

    @app.action("factory_scrap_reject")
    def scrap_reject(ack, body, client):
        ack()
        sid = int(body["actions"][0]["value"])

        def work(admin):
            factory.reject_scrap(sid)
            return f"❌ {admin} 已驳回，库存不变"
        _decide(body, client, ("scrap", sid), work, "⏳ 待批准", "❌ 已驳回")

    # Stock count — waits for an admin unless nothing differs
    @app.view("factory_count_modal")
    def submit_count(ack, body, client, view):
        v = view["state"]["values"]
        counts = []
        for block, val in v.items():
            if block.startswith("c_") and val["v"].get("value") not in (None, ""):
                counts.append((int(block[2:]), float(val["v"]["value"])))
        if not counts:
            ack(response_action="errors", errors={"location": "至少填一项数到的数量"})
            return
        ack()
        user_id = body["user"]["id"]
        location_id = _meta(view).get("location_id")

        def _run():
            who = _name(client, user_id)
            try:
                rows = factory.submit_count(counts, who)
                loc = next(factory._short_loc(l["complete_name"]) for l in factory.internal_locations() if l["id"] == location_id)
                diffs = [r for r in rows if abs(r["diff"]) > 1e-6]
                ids = [r["quant_id"] for r in rows]
                if not diffs:
                    factory.apply_count(ids, who)
                    client.chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE,
                                            text=f"📋 *盘点 {loc}* ✅ {len(rows)} 项全部与系统一致（提交人 {who}）")
                    return
                lines = [f"• `{r['lot']}` {r['product']}：系统 {r['qty']:g} → 实盘 *{r['counted']:g}* {r['uom']}（{r['diff']:+g}）"
                         for r in diffs]
                text = (f"📋 *盘点 {loc}* ⏳ 待批准\n共 {len(rows)} 项，{len(diffs)} 项有差异：\n" + "\n".join(lines))
                client.chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE, text="盘点待批准", blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": f"提交人 {who} · 批准后系统库存改成实盘数"}]},
                    _approval_buttons("factory_count", json.dumps(ids))])
            except Exception as e:
                _fail(client, user_id, who, "盘点", e)

        threading.Thread(target=_run, daemon=True).start()

    @app.action("factory_count_approve")
    def count_approve(ack, body, client):
        ack()
        ids = json.loads(body["actions"][0]["value"])

        def work(admin):
            factory.apply_count(ids, admin)
            return f"✅ {admin} 已批准，系统库存已按实盘调整"
        _decide(body, client, ("count", tuple(ids)), work, "⏳ 待批准", "✅ 已批准")

    @app.action("factory_count_reject")
    def count_reject(ack, body, client):
        ack()
        ids = json.loads(body["actions"][0]["value"])

        def work(admin):
            factory.clear_count(ids)
            return f"❌ {admin} 已驳回，系统库存不变（请重新盘点）"
        _decide(body, client, ("count", tuple(ids)), work, "⏳ 待批准", "❌ 已驳回")

    # Production — done at once
    @app.view("factory_produce_modal")
    def submit_produce(ack, body, client, view):
        v, errors = view["state"]["values"], {}
        qty = _qty_value(v, "qty", errors)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        user_id = body["user"]["id"]
        bom_id = _selected(v, "bom")
        note = (v.get("note", {}).get("v", {}).get("value") or "").strip()

        def _run():
            who = _name(client, user_id)
            try:
                mo_id = factory.produce(bom_id, qty, who, note)
                s = factory.production_summary(mo_id)
                text = (f"🏭 *生产入库 {s['name']}* ✅ 已入库\n{s['product']} — *{s['qty']:g} {s['uom']}*\n"
                        f"成品批号：{', '.join(s['lots']) or '—'}\n用料：\n" + "\n".join(f"• {u}" for u in s["used"])
                        + (f"\n备注：{note}" if note else ""))
                client.chat_postMessage(channel=Config.SLACK_CHANNEL_PRODUCTION or Config.SLACK_CHANNEL_WAREHOUSE,
                                        text=f"生产入库 {s['name']}", blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": f"提交人 {who}"}]}])
            except Exception as e:
                _fail(client, user_id, who, "生产入库", e)

        threading.Thread(target=_run, daemon=True).start()

    # Stock query — answer inside the modal
    @app.view("factory_query_modal")
    def submit_query(ack, body, client, view):
        pid = _selected(view["state"]["values"], "product")
        rows = [q for q in factory.stock_quants(product_id=pid, limit=200)] if pid else []
        if not rows:
            text = "没有库存。"
        else:
            total = sum(q["qty"] for q in rows)
            text = f"*{rows[0]['product']}*\n合计 *{total:g} {rows[0]['uom']}*\n" + "\n".join(
                f"• `{q['lot'] or '无批号'}` · {q['location']} · {q['qty']:g}" + (f" · 到期 {q['expiry']}" if q["expiry"] else "")
                for q in rows)
        ack(response_action="update", view=_modal("factory_query_done", "库存", None,
                                                   [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}], {}))
