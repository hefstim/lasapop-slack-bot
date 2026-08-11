"""Slack action handlers — processes button clicks and @mention commands."""
import logging
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
    "`@OdooBot contactsync` — Sync Customer Contact Info sheet → Odoo\n"
    "`@OdooBot help` — Show this help message"
)


def register_handlers(app: App):

    # ── @mention commands ─────────────────────────────────────────────────────

    # `app_mention` on this Slack app fires unreliably — sometimes not at all
    # (historically, zero occurrences of the mention-only ack text like
    # ":hourglass_flowing_sand: Running" across days of bot.log), sometimes
    # redelivered twice for one real message (observed directly: two
    # `hr.expense` records created 1.2s apart from a single `expense` mention).
    # A `message`-event fallback is also registered below so mentions still
    # get through when `app_mention` stays silent. Since either or both may
    # fire for the same underlying message, every dispatch is deduplicated by
    # (channel, ts) before doing anything with side effects (Odoo writes,
    # scheduler jobs, Slack posts).
    _seen_event_ids: set[str] = set()
    _seen_event_order: list[str] = []
    _seen_lock = threading.Lock()
    _SEEN_CAP = 500

    def _already_processed(channel: str, ts: str) -> bool:
        key = f"{channel}:{ts}"
        # Bolt can invoke listeners for concurrent events on separate threads —
        # without the lock, two near-simultaneous deliveries of the same event
        # could both read "not seen yet" before either finishes writing it,
        # defeating the whole point of this guard.
        with _seen_lock:
            if key in _seen_event_ids:
                return True
            _seen_event_ids.add(key)
            _seen_event_order.append(key)
            if len(_seen_event_order) > _SEEN_CAP:
                oldest = _seen_event_order.pop(0)
                _seen_event_ids.discard(oldest)
            return False

    @app.event("app_mention")
    def handle_mention(body, say, client):
        _dispatch_mention(body.get("event", {}), say, client)

    _bot_id_cache: dict[str, str] = {}

    def _get_bot_id(client) -> str:
        # Slack renders a mention as <@USERID>, but if a user copy-pastes an
        # older message instead of retyping "@" and picking from autocomplete,
        # the client can carry over a <@BOTID> reference instead (observed
        # directly in #exp-approval: two `expense` submissions with
        # <@B0BG01CFK8U> instead of <@U0BFW9JPX7U> that silently never
        # dispatched). Treat both forms as a valid mention.
        if "id" not in _bot_id_cache:
            try:
                _bot_id_cache["id"] = client.auth_test().get("bot_id", "")
            except Exception:
                _bot_id_cache["id"] = ""
        return _bot_id_cache["id"]

    @app.event("message")
    def handle_message_mention(body, say, client, context):
        event = body.get("event", {})
        if event.get("subtype") or event.get("bot_id"):
            return  # ignore edits, deletions, and our own bot's messages
        bot_user_id = context.get("bot_user_id", "")
        bot_id = _get_bot_id(client)
        text = event.get("text", "")
        mentioned = (bot_user_id and f"<@{bot_user_id}>" in text) or (bot_id and f"<@{bot_id}>" in text)
        if not mentioned:
            return
        _dispatch_mention(event, say, client)

    def _dispatch_mention(event, say, client):
        if _already_processed(event.get("channel", ""), event.get("ts", "")):
            logger.info("Skipping duplicate mention event: channel=%s ts=%s",
                        event.get("channel"), event.get("ts"))
            return
        text    = event.get("text", "")
        channel = event.get("channel", "")

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
