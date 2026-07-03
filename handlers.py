"""Slack action handlers — processes button clicks and @mention commands."""
import logging
import re
import threading
from slack_bolt import App
from config import Config
import db

logger = logging.getLogger(__name__)

# ── @mention command map ──────────────────────────────────────────────────────

COMMANDS = {
    "daily":      ("Daily sales report", "job_daily_report"),
    "report":     ("Daily sales report", "job_daily_report"),
    "weekly":     ("Weekly sales report", "job_weekly_report"),
    "monthly":    ("Last month report", "job_monthly_report"),
    "thismonth":  ("Current month report", "job_current_month_report"),
    "dormant":    ("Dormant customer export to Google Sheet", "job_dormant_customers"),
    "customers":  ("Dormant customer export to Google Sheet", "job_dormant_customers"),
    "invoices":   ("Overdue invoice check", "job_overdue_invoices"),
    "invoicesheet": ("Export overdue invoices (≤30 days) to Google Sheet for follow-up", "job_export_overdue_invoices"),
    "legacy":     ("Legacy overdue invoices export (manual review)", "job_export_legacy_invoices"),
    "quotations": ("Pending quotation check", "job_pending_quotations"),
    "quotes":     ("Pending quotation check", "job_pending_quotations"),
    "stock":      ("Low stock alert", "job_low_stock"),
}

HELP_TEXT = (
    "*Available commands — mention me with any of these:*\n\n"
    "`@OdooBot report` — Daily sales report\n"
    "`@OdooBot weekly` — Weekly sales report\n"
    "`@OdooBot monthly` — Last month's completed report\n"
    "`@OdooBot monthly 2025-11` — Report for a specific month\n"
    "`@OdooBot thismonth` — Current month progress report\n"
    "`@OdooBot dormant` — Export dormant customers to Google Sheet\n"
    "`@OdooBot invoices` — Overdue invoice check\n"
    "`@OdooBot invoicesheet` — Export overdue invoices (≤30 days) to Google Sheet for follow-up\n"
    "`@OdooBot legacy` — Export invoices overdue >30 days to Google Sheet for manual review\n"
    "`@OdooBot quotations` — Pending quotation follow-up\n"
    "`@OdooBot stock` — Low stock alert\n"
    "`@OdooBot stockcheck` — Compare weekly inventory Sheet vs Odoo (all stores)\n"
    "`@OdooBot stockcheck qc` — Inventory check for QC only (antipolo / qc / bulacan)\n"
    "`@OdooBot contactsync` — Sync Customer Contact Info sheet → Odoo\n"
    "`@OdooBot help` — Show this help message"
)


def register_handlers(app: App):

    # ── @mention commands ─────────────────────────────────────────────────────

    @app.event("app_mention")
    def handle_mention(body, say, client):
        text    = body.get("event", {}).get("text", "")
        channel = body["event"]["channel"]

        words   = text.split()
        keyword = words[1].strip("<>@!").rstrip(".,!?").lower() if len(words) > 1 else ""
        # Optional third word: a month argument like "2025-11" or "2025-05"
        month_arg = words[2].strip() if len(words) > 2 else ""

        if keyword == "help" or not keyword:
            say(HELP_TEXT)
            return

        # ── Monthly report with specific month: @OdooBot monthly 2025-11 ──────
        if keyword in ("monthly", "thismonth") and month_arg:
            try:
                from datetime import datetime as _dt
                parsed = _dt.strptime(month_arg, "%Y-%m")
                y, m = parsed.year, parsed.month
            except ValueError:
                say(f":x: Invalid month format `{month_arg}`. Please use `YYYY-MM`, e.g. `2025-11`.")
                return

            label = f"Report for {month_arg}"
            ack_msg = client.chat_postMessage(
                channel=channel,
                text=f":hourglass_flowing_sand: Fetching *{label}*... Please wait.",
            )

            def _run_specific(y=y, m=m, label=label):
                try:
                    from odoo.reports import get_monthly_report
                    from slack.messages import build_monthly_report_blocks
                    from slack_sdk import WebClient as _WC
                    from config import Config as _Cfg
                    _slack = _WC(token=_Cfg.SLACK_BOT_TOKEN)
                    data = get_monthly_report(year=y, month=m)
                    _slack.chat_postMessage(
                        channel=channel,
                        blocks=build_monthly_report_blocks(data, target=_Cfg.MONTHLY_SALES_TARGET),
                        text=f"Sales Report — {data['month_label']}",
                    )
                    client.chat_update(
                        channel=channel,
                        ts=ack_msg["ts"],
                        text=f":white_check_mark: *{label}* completed.",
                    )
                except Exception as e:
                    logger.error("monthly %s-%s failed: %s", y, m, e)
                    client.chat_update(
                        channel=channel,
                        ts=ack_msg["ts"],
                        text=f":x: Failed to fetch {label}: {e}",
                    )

            threading.Thread(target=_run_specific, daemon=True).start()
            return

        # ── contactsync: read Customer Contact Info sheet → write to Odoo ──────
        if keyword == "contactsync":
            ack_msg = client.chat_postMessage(
                channel=channel,
                text=":hourglass_flowing_sand: Syncing *Customer Contact Info* sheet to Odoo...",
            )

            def _run_contactsync():
                try:
                    from google_sheets.contact_exporter import sync_contact_info_to_odoo
                    updated, skipped, errors = sync_contact_info_to_odoo()
                    lines = [f":white_check_mark: Sync complete — *{updated}* updated, *{skipped}* skipped (no data)."]
                    if errors:
                        lines.append(":warning: Errors:")
                        lines.extend(f"  • {e}" for e in errors[:10])
                    client.chat_update(
                        channel=channel,
                        ts=ack_msg["ts"],
                        text="\n".join(lines),
                    )
                except Exception as e:
                    logger.error("contactsync failed: %s", e)
                    client.chat_update(
                        channel=channel,
                        ts=ack_msg["ts"],
                        text=f":x: contactsync failed: {e}",
                    )

            threading.Thread(target=_run_contactsync, daemon=True).start()
            return

        # ── stockcheck: compare Sheet inventory counts vs Odoo ───────────────
        if keyword == "stockcheck":
            branch_arg = words[2].strip().lower() if len(words) > 2 else "all"
            branches = (
                ["antipolo", "qc"]
                if branch_arg in ("all", "")
                else [branch_arg]
            )
            label = f"Stock check ({', '.join(b.upper() for b in branches)})"
            ack_msg = client.chat_postMessage(
                channel=channel,
                text=f":hourglass_flowing_sand: Running *{label}*... Reading Sheet & Odoo.",
            )

            def _run_stockcheck(branches=branches, label=label):
                try:
                    _do_stockcheck(client, channel, ack_msg["ts"], branches)
                except Exception as e:
                    logger.error("stockcheck failed: %s", e)
                    client.chat_update(
                        channel=channel,
                        ts=ack_msg["ts"],
                        text=f":x: Stock check failed: {e}",
                    )

            threading.Thread(target=_run_stockcheck, daemon=True).start()
            return

        if keyword not in COMMANDS:
            say(f"Unknown command `{keyword}`. Type `@OdooBot help` to see available commands.")
            return

        label, job_fn_name = COMMANDS[keyword]

        ack_msg = client.chat_postMessage(
            channel=channel,
            text=f":hourglass_flowing_sand: Running *{label}*... Please wait.",
        )

        def _run():
            try:
                from scheduler import jobs as _jobs
                fn = getattr(_jobs, job_fn_name)
                fn()
                client.chat_update(
                    channel=channel,
                    ts=ack_msg["ts"],
                    text=f":white_check_mark: *{label}* completed.",
                )
            except Exception as e:
                logger.error("Command '%s' failed: %s", keyword, e)
                client.chat_update(
                    channel=channel,
                    ts=ack_msg["ts"],
                    text=f":x: *{label}* failed: {e}",
                )

        threading.Thread(target=_run, daemon=True).start()

    # ── Customer actions ──────────────────────────────────────────────────────

    @app.action("customer_contacted")
    def handle_customer_contacted(ack, body, client):
        ack()
        _update_customer(body, client, "contacted")

    @app.action("customer_scheduled")
    def handle_customer_scheduled(ack, body, client):
        ack()
        _update_customer(body, client, "scheduled")

    @app.action("customer_skipped")
    def handle_customer_skipped(ack, body, client):
        ack()
        _update_customer(body, client, "skipped")

    @app.action("customer_excluded")
    def handle_customer_excluded(ack, body, client):
        ack()
        user_id = body.get("user", {}).get("id", "")
        if user_id not in Config.ADMIN_USER_IDS:
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="⛔ Only the admin can permanently exclude customers.",
            )
            return
        row_id = int(body["actions"][0]["value"])
        actor = _get_actor(body)
        row = db.get_row("customer_reminders", row_id)
        if row:
            db.exclude_customer(row["partner_id"], row["partner_name"], actor)
            db.update_status("customer_reminders", row_id, "excluded")
            new_text = f"~{row['partner_name']}~ — 🚫 Won't Buy Again  _(by {actor})_"
            try:
                client.chat_update(
                    channel=body["channel"]["id"],
                    ts=body["message"]["ts"],
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}],
                    text=new_text,
                )
            except Exception as e:
                logger.error("Failed to update message: %s", e)

    # ── Invoice actions ───────────────────────────────────────────────────────

    @app.action("invoice_followed")
    def handle_invoice_followed(ack, body, client):
        ack()
        _update_generic(body, client, "invoice_reminders", "followed", "✅ 已跟进")

    @app.action("invoice_called")
    def handle_invoice_called(ack, body, client):
        ack()
        _update_generic(body, client, "invoice_reminders", "called", "📞 已记录通话")

    @app.action("invoice_skipped")
    def handle_invoice_skipped(ack, body, client):
        ack()
        _update_generic(body, client, "invoice_reminders", "skipped", "⏭ 已跳过")

    # ── Quotation actions ─────────────────────────────────────────────────────

    @app.action("quote_followed")
    def handle_quote_followed(ack, body, client):
        ack()
        _update_generic(body, client, "quotation_reminders", "followed", "✅ 已跟进")

    @app.action("quote_closed")
    def handle_quote_closed(ack, body, client):
        ack()
        _update_generic(body, client, "quotation_reminders", "closed", "❌ 已关闭报价")

    @app.action("quote_skipped")
    def handle_quote_skipped(ack, body, client):
        ack()
        _update_generic(body, client, "quotation_reminders", "skipped", "⏭ 已跳过")

    # ── Stock actions ─────────────────────────────────────────────────────────

    @app.action("stock_handled")
    def handle_stock_handled(ack, body, client):
        ack()
        _update_generic(body, client, "stock_alerts", "handled", "✅ 已处理")

    @app.action("stock_ignored")
    def handle_stock_ignored(ack, body, client):
        ack()
        _update_generic(body, client, "stock_alerts", "ignored", "⏭ 已忽略")

    # ── 现金支出核对（监听现金频道的普通消息）─────────────────────────────────

    @app.event("message")
    def handle_cash_expense_message(body, client):
        event = body.get("event", {})
        if not Config.SLACK_CHANNEL_CASH:
            return
        if event.get("channel") != Config.SLACK_CHANNEL_CASH:
            return
        # 跳过 bot 自身消息和系统消息（编辑、删除、加入等）
        if event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text", "").strip()
        if not text:
            return
        threading.Thread(
            target=_verify_cash_expense,
            args=(event, client, text),
            daemon=True,
        ).start()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_actor(body: dict) -> str:
    user = body.get("user", {})
    return user.get("name") or user.get("id") or "unknown"


def _update_customer(body: dict, client, action: str):
    row_id = int(body["actions"][0]["value"])
    actor = _get_actor(body)
    db.update_status("customer_reminders", row_id, action)

    row = db.get_row("customer_reminders", row_id)
    if not row:
        return

    labels = {
        "contacted": "✅ 已联系",
        "scheduled": "📅 已安排跟进",
        "skipped": "⏭ 已跳过",
    }
    label = labels.get(action, action)
    new_text = f"~{row['partner_name']}~ — {label}  _(by {actor})_"

    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": new_text}}
            ],
            text=new_text,
        )
    except Exception as e:
        logger.error("Failed to update Slack message: %s", e)


def _update_generic(body: dict, client, table: str, status: str, label: str):
    row_id = int(body["actions"][0]["value"])
    actor = _get_actor(body)
    db.update_status(table, row_id, status)

    row = db.get_row(table, row_id)
    record_name = ""
    if row:
        # Try common name columns
        for col in ("invoice_name", "order_name", "product_name", "partner_name"):
            if row[col]:
                record_name = row[col]
                break

    new_text = f"~{record_name}~ — {label}  _(by {actor})_"
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": new_text}}
            ],
            text=new_text,
        )
    except Exception as e:
        logger.error("Failed to update Slack message: %s", e)


def _verify_cash_expense(event: dict, client, text: str):
    from google_sheets.cash_reader import detect_branch, get_daily_expenses

    channel = event["channel"]
    thread_ts = event["ts"]

    branch = detect_branch(text)
    if not branch:
        return  # 没有门店名，忽略（避免误触发普通聊天消息）

    # 提取金额：去掉货币符号和逗号后，取最后一个数字作为合计
    cleaned = text.replace("₱", "").replace(",", "")
    raw_numbers = re.findall(r"\d+(?:\.\d+)?", cleaned)
    numbers = [float(n) for n in raw_numbers if float(n) >= 1]
    if not numbers:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"⚠️ I see *{branch.upper()}* but couldn't find an amount. "
                "Please include the total, e.g. `QC: 2,865`"
            ),
        )
        return

    reported = numbers[-1]

    try:
        data = get_daily_expenses(branch)
    except Exception as e:
        logger.error("_verify_cash_expense sheet read failed: %s", e)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"⚠️ Could not read Google Sheet for *{branch.upper()}*: {e}",
        )
        return

    if "error" in data:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"⚠️ Sheet error for *{branch.upper()}*: {data['error']}",
        )
        return

    if not data["found"]:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"⚠️ *{branch.upper()}* — No expense entries found in Sheet for today "
                f"({data['date']}). Please make sure the Sheet is updated first."
            ),
        )
        return

    sheet_total = data["total"]
    diff = abs(reported - sheet_total)
    branch_display = {"qc": "QC", "antipolo": "Antipolo", "bulacan": "Bulacan"}.get(
        branch, branch.upper()
    )

    if diff < 1.0:
        reply = (
            f"✅ *{branch_display}* — Reported ₱{reported:,.2f}  ·  "
            f"Sheet ₱{sheet_total:,.2f}  →  *Match!*"
        )
    else:
        lines = [
            f"⚠️ *{branch_display}* — Discrepancy detected",
            f"Reported: *₱{reported:,.2f}*   Sheet: *₱{sheet_total:,.2f}*   Diff: ₱{diff:,.2f}",
            "",
            "*Sheet breakdown today:*",
        ]
        for cat, amt in sorted(data["by_category"].items(), key=lambda x: -x[1]):
            lines.append(f"  • {cat}: ₱{amt:,.2f}")
        reply = "\n".join(lines)

    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply)
    logger.info(
        "cash verify: branch=%s reported=%.2f sheet=%.2f diff=%.2f",
        branch, reported, sheet_total, diff,
    )


# ── 库存盘点对比 ───────────────────────────────────────────────────────────────

def _do_stockcheck(client, channel: str, ack_ts: str, branches: list[str]):
    from datetime import date
    from google_sheets.stock_sheet import read_inventory_counts
    from odoo.stock import get_odoo_stock

    branch_display = {"antipolo": "Antipolo", "qc": "QC", "bulacan": "Bulacan"}
    today = date.today().strftime("%Y-%m-%d")
    all_lines = [f"📦 *Stock Count Verification — {today}*\n"]
    any_issue = False

    for branch in branches:
        bname = branch_display.get(branch, branch.upper())
        all_lines.append(f"*{bname}*")

        try:
            sheet_counts = read_inventory_counts(branch)
        except Exception as e:
            all_lines.append(f"  ⚠️ Cannot read Sheet: {e}")
            all_lines.append("")
            continue

        if not sheet_counts:
            all_lines.append("  _(no counts filled in yet)_")
            all_lines.append("")
            continue

        try:
            odoo_stock = get_odoo_stock(branch)
        except Exception as e:
            all_lines.append(f"  ⚠️ Cannot read Odoo: {e}")
            all_lines.append("")
            continue

        matches, diffs, not_found = [], [], []

        for name, info in sorted(sheet_counts.items()):
            counted = info["counted"]
            unit    = info["unit"]
            notes   = info["notes"]

            odoo_entry = odoo_stock.get(name)
            if odoo_entry is None:
                not_found.append(f"  ❓ `{name}` — not found in Odoo")
                continue

            odoo_qty = odoo_entry["qty"]
            diff     = counted - odoo_qty

            note_str = f"  _{notes}_" if notes else ""
            if abs(diff) < 0.5:
                matches.append(f"  ✅ {name}: {counted:,.0f} {unit}")
            else:
                sign = "+" if diff > 0 else ""
                diffs.append((abs(diff), f"  ⚠️ *{name}*: counted={counted:,.0f}  Odoo={odoo_qty:,.0f}  diff={sign}{diff:,.0f} {unit}{note_str}"))
                any_issue = True

        # 差异从大到小排序
        diffs.sort(key=lambda x: -x[0])

        if diffs:
            all_lines.append(f"  *{len(diffs)} discrepancies:*")
            all_lines.extend(d[1] for d in diffs)

        if matches:
            all_lines.append(f"  ✅ {len(matches)} product(s) matched")

        if not_found:
            all_lines.append(f"  *{len(not_found)} product name(s) not found in Odoo:*")
            all_lines.extend(not_found)

        uncounted = len(odoo_stock) - len(sheet_counts)
        if uncounted > 0:
            all_lines.append(f"  _(+{uncounted} Odoo products not counted)_")

        all_lines.append("")

    if not any_issue and len(branches) > 0:
        all_lines.append("_All counted products match Odoo_ ✅")

    # Slack 单条消息最大 3000 字符，超长则分段发
    full_text = "\n".join(all_lines)
    if len(full_text) <= 3000:
        client.chat_update(channel=channel, ts=ack_ts, text=full_text)
    else:
        # 先更新 ack 消息，再追加
        client.chat_update(channel=channel, ts=ack_ts, text=full_text[:3000])
        remaining = full_text[3000:]
        while remaining:
            chunk, remaining = remaining[:3000], remaining[3000:]
            client.chat_postMessage(channel=channel, text=chunk)

    logger.info("stockcheck: branches=%s any_issue=%s", branches, any_issue)
