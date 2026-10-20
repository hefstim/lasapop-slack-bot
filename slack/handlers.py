"""Slack action handlers — processes button clicks and @mention commands."""
import logging
import threading
from slack_bolt import App
from config import Config
from slack.messages import build_expense_approval_blocks
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

if Config.PETTY_CASH_ENABLED:
    HELP_TEXT = (
        "*Available commands — mention me with any of these:*\n\n"
        "`@lasapop_bot expense <amount> <description>` — Submit an expense for approval (attach a receipt photo if you have one); "
        "the approver picks the category\n"
        "`@lasapop_bot expensetotal` — This month's spent expenses by employee and category + petty cash balance\n"
        "`@lasapop_bot balance` — Current petty cash balance (from Odoo)\n"
        "`@lasapop_bot cashin` — Top-ups are recorded through the AI assistant; this command only reminds you\n"
        "`@lasapop_bot help` — Show this help message\n\n"
        "*Or click the ⚡ shortcuts icon next to the message box:*\n"
        "  • *提交支出* — submit an expense as a form: choose a category, upload the receipt (recommended)\n"
        "  • *查询现金统计* — same as `expensetotal`, posted in #exp-approval\n"
        "  • *充值现金* — reminds you to record top-ups through the AI assistant"
    )
else:
    HELP_TEXT = (
        "*Available commands — mention me with any of these:*\n\n"
        "`@lasapop_bot expense <amount> <description>` — Submit an expense for approval (attach a receipt photo if you have one)\n"
        "`@lasapop_bot expensetotal` — This month's approved cash expense total + current cash balance\n"
        "`@lasapop_bot cashin <amount> <note>` — Record a cash top-up into the factory cash fund (admin only)\n"
        "`@lasapop_bot balance` — Current factory cash balance (top-ups minus approved expenses)\n"
        "`@lasapop_bot help` — Show this help message\n\n"
        "*Or click the ⚡ shortcuts icon next to the message box:*\n"
        "  • *提交支出* — same as `expense`, as a form (supports receipt upload)\n"
        "  • *查询现金统计* — same as `expensetotal`/`balance`, posted in #exp-approval\n"
        "  • *充值现金* — same as `cashin`, as a form (admin only)"
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

        # ── Monthly report with specific month: @lasapop_bot monthly 2025-11 ──────
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

        # ── expense: submit an expense report for approval ────────────────────
        if keyword == "expense":
            submitter_id = event.get("user", "")
            employee_id = Config.EXPENSE_EMPLOYEE_MAP.get(submitter_id)
            if not employee_id:
                say("⛔ You're not registered as an employee for expense submission. Contact an admin.")
                return
            if len(words) < 4:
                say("Usage: `@lasapop_bot expense <amount> <description>` — e.g. `@lasapop_bot expense 500 taxi fare`")
                return
            try:
                amount = float(words[2])
                if amount <= 0:
                    raise ValueError
            except ValueError:
                say(f"⛔ Invalid amount `{words[2]}`. Must be a positive number.")
                return
            description = " ".join(words[3:])
            try:
                employee_name = client.users_info(user=submitter_id)["user"]["real_name"]
            except Exception:
                employee_name = submitter_id

            # Receipt photo is optional — attach whichever images were shared
            # alongside the mention, if any. No receipt is a valid submission.
            image_files = [
                f for f in event.get("files", [])
                if (f.get("mimetype") or "").startswith("image/")
            ]

            threading.Thread(
                target=_submit_and_post_expense,
                args=(client, employee_id, employee_name, amount, description, image_files, say),
                kwargs={"submitter_id": submitter_id},
                daemon=True,
            ).start()
            return

        # ── expensetotal: this month's approved cash expense summary ──────────
        if keyword == "expensetotal":
            threading.Thread(target=_report_cash_stats, args=(say,), daemon=True).start()
            return

        # ── cashin: record a cash top-up into the factory cash fund (admin only) ──
        if keyword == "cashin":
            submitter_id = event.get("user", "")
            if submitter_id not in Config.ADMIN_USER_IDS:
                say("⛔ Only an admin can record a cash top-up.")
                return
            if len(words) < 3:
                say("Usage: `@lasapop_bot cashin <amount> <note>` — e.g. `@lasapop_bot cashin 50000 monthly withdrawal`")
                return
            try:
                amount = float(words[2])
                if amount <= 0:
                    raise ValueError
            except ValueError:
                say(f"⛔ Invalid amount `{words[2]}`. Must be a positive number.")
                return
            note = " ".join(words[3:])
            _record_cash_topup(amount, note, submitter_id, say)
            return

        # ── balance: current factory cash balance ──────────────────────────────
        if keyword == "balance":
            threading.Thread(target=_report_balance, args=(say,), daemon=True).start()
            return

        if keyword not in COMMANDS:
            say(f"Unknown command `{keyword}`. Type `@lasapop_bot help` to see available commands.")
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

    # ── Expense actions ────────────────────────────────────────────────────────

    @app.action("expense_approved")
    def handle_expense_approved(ack, body, client):
        ack()
        _update_expense(body, client, "approved")

    @app.action("expense_refused")
    def handle_expense_refused(ack, body, client):
        ack()
        _update_expense(body, client, "refused")

    # Buttons on the ephemeral "possible duplicate" prompt (see _ask_duplicate_confirmation).
    @app.action("expense_approve_duplicate")
    def handle_expense_approve_duplicate(ack, body, client, respond):
        ack()
        respond(delete_original=True)
        _update_expense(body, client, "approved", confirm_duplicate=True)

    @app.action("expense_duplicate_cancel")
    def handle_expense_duplicate_cancel(ack, respond):
        ack()
        respond(delete_original=True)

    # Category dropdown on the approval card: nothing to do on change — the
    # Approve handler reads the selection from the message state.
    @app.action("expense_category_select")
    def handle_expense_category_select(ack):
        ack()

    # ── Expense submission shortcut (⚡ shortcuts icon → 提交支出) ──────────────
    # Alternative to the `expense` @mention command — a form instead of typing
    # a fixed-format command, and sidesteps the mention-format issues that
    # plagued the @mention path (copy-pasted <@BOTID> mentions, flaky event
    # delivery — see project notes). Uses views_open/view_submission, a
    # separate Slack dispatch mechanism from message/app_mention events.
    @app.shortcut("submit_expense")
    def handle_submit_expense_shortcut(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if user_id not in Config.EXPENSE_EMPLOYEE_MAP:
            client.chat_postMessage(
                channel=user_id,
                text="⛔ You're not registered as an employee for expense submission. Contact an admin.",
            )
            return
        category_blocks = []
        if Config.PETTY_CASH_ENABLED:
            from odoo.expenses import EXPENSE_CATEGORIES
            category_blocks = [{
                "type": "input",
                "block_id": "category_block",
                "label": {"type": "plain_text", "text": "类别 / Category"},
                "element": {
                    "type": "static_select",
                    "action_id": "category_input",
                    "placeholder": {"type": "plain_text", "text": "选择类别"},
                    "options": [{"text": {"type": "plain_text", "text": label}, "value": code}
                                for code, label in EXPENSE_CATEGORIES],
                },
            }]
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "expense_modal",
                "title": {"type": "plain_text", "text": "提交支出"},
                "submit": {"type": "plain_text", "text": "提交"},
                "close": {"type": "plain_text", "text": "取消"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "amount_block",
                        "label": {"type": "plain_text", "text": "金额 (₱)"},
                        "element": {"type": "plain_text_input", "action_id": "amount_input"},
                    },
                    *category_blocks,
                    {
                        "type": "input",
                        "block_id": "desc_block",
                        "label": {"type": "plain_text", "text": "描述"},
                        "element": {"type": "plain_text_input", "action_id": "desc_input"},
                    },
                    {
                        "type": "input",
                        "block_id": "receipt_block",
                        "optional": True,
                        "label": {"type": "plain_text", "text": "收据（可选）"},
                        "element": {
                            "type": "file_input",
                            "action_id": "receipt_input",
                            "filetypes": ["png", "jpg", "jpeg", "pdf"],
                            "max_files": 1,
                        },
                    },
                ],
            },
        )

    @app.view("expense_modal")
    def handle_expense_modal_submission(ack, body, client, view):
        user_id = body["user"]["id"]
        values = view["state"]["values"]
        amount_raw = values["amount_block"]["amount_input"]["value"].strip()
        description = values["desc_block"]["desc_input"]["value"].strip()
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            ack(response_action="errors", errors={"amount_block": "请输入有效金额"})
            return
        ack()

        employee_id = Config.EXPENSE_EMPLOYEE_MAP.get(user_id)
        if not employee_id:
            client.chat_postMessage(
                channel=user_id,
                text="⛔ You're not registered as an employee for expense submission. Contact an admin.",
            )
            return

        receipt_files = (values.get("receipt_block") or {}).get("receipt_input", {}).get("files") or []
        category_code = (((values.get("category_block") or {}).get("category_input") or {})
                         .get("selected_option") or {}).get("value", "")

        def _notify(text):
            client.chat_postMessage(channel=Config.SLACK_CHANNEL_EXPENSE_APPROVAL, text=text)

        try:
            employee_name = client.users_info(user=user_id)["user"]["real_name"]
        except Exception:
            employee_name = user_id

        threading.Thread(
            target=_submit_and_post_expense,
            args=(client, employee_id, employee_name, amount, description, receipt_files, _notify),
            kwargs={"submitter_id": user_id, "category_code": category_code},
            daemon=True,
        ).start()

    # ── Cash stats shortcut (⚡ shortcuts icon → 查询现金统计) ─────────────────
    # Read-only — same info as `expensetotal`, DMed to whoever clicked it.
    @app.shortcut("query_cash_stats")
    def handle_query_cash_stats_shortcut(ack, body, client):
        ack()
        threading.Thread(
            target=_report_cash_stats,
            args=(lambda text: client.chat_postMessage(channel=Config.SLACK_CHANNEL_EXPENSE_APPROVAL, text=text),),
            daemon=True,
        ).start()

    # ── Cash top-up shortcut (⚡ shortcuts icon → 充值现金, admin only) ────────
    @app.shortcut("cashin_shortcut")
    def handle_cashin_shortcut(ack, body, client):
        ack()
        user_id = body["user"]["id"]
        if user_id not in Config.ADMIN_USER_IDS:
            client.chat_postMessage(channel=user_id, text="⛔ Only an admin can record a cash top-up.")
            return
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "cashin_modal",
                "title": {"type": "plain_text", "text": "充值现金"},
                "submit": {"type": "plain_text", "text": "提交"},
                "close": {"type": "plain_text", "text": "取消"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "amount_block",
                        "label": {"type": "plain_text", "text": "金额 (₱)"},
                        "element": {"type": "plain_text_input", "action_id": "amount_input"},
                    },
                    {
                        "type": "input",
                        "block_id": "note_block",
                        "optional": True,
                        "label": {"type": "plain_text", "text": "备注"},
                        "element": {"type": "plain_text_input", "action_id": "note_input"},
                    },
                ],
            },
        )

    @app.view("cashin_modal")
    def handle_cashin_modal_submission(ack, body, client, view):
        user_id = body["user"]["id"]
        if user_id not in Config.ADMIN_USER_IDS:
            ack()
            client.chat_postMessage(channel=user_id, text="⛔ Only an admin can record a cash top-up.")
            return
        values = view["state"]["values"]
        amount_raw = values["amount_block"]["amount_input"]["value"].strip()
        note = ((values.get("note_block") or {}).get("note_input", {}).get("value") or "").strip()
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            ack(response_action="errors", errors={"amount_block": "请输入有效金额"})
            return
        ack()
        _record_cash_topup(
            amount, note, user_id,
            lambda text: client.chat_postMessage(channel=Config.SLACK_CHANNEL_EXPENSE_APPROVAL, text=text),
        )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _submit_and_post_expense(client, employee_id, employee_name, amount, description, raw_files, notify,
                             submitter_id="", category_code=""):
    """Shared by the `expense` @mention command and the `submit_expense`
    shortcut modal. `raw_files` items just need a `url_private` (or an `id`
    to resolve one via files_info) and optionally a `name`. `notify(text)`
    delivers the final result back to whichever surface triggered this.
    `submitter_id` is the Slack user id of whoever submitted, kept for the
    audit trail and so approval can later check against self-approval.
    `category_code` (petty cash only) pre-selects the category on the card;
    the @mention command has none, so the approver picks it."""
    try:
        from odoo.expenses import create_and_submit_expense, attach_receipt
        expense_id = create_and_submit_expense(employee_id, amount, description, category_code=category_code)

        attached = 0
        for f in raw_files:
            try:
                url = f.get("url_private")
                name = f.get("name", "receipt.jpg")
                if not url and f.get("id"):
                    info = client.files_info(file=f["id"])["file"]
                    url = info.get("url_private")
                    name = info.get("name", name)
                if not url:
                    continue
                import requests
                resp_file = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {Config.SLACK_BOT_TOKEN}"},
                    timeout=15,
                )
                resp_file.raise_for_status()
                attach_receipt(expense_id, name, resp_file.content)
                attached += 1
            except Exception as e:
                logger.error("Failed to attach receipt for expense %s: %s", expense_id, e)

        row_id = db.save_expense_request(expense_id, employee_name, amount, description, submitter_id, "", "")
        resp = client.chat_postMessage(
            channel=Config.SLACK_CHANNEL_EXPENSE_APPROVAL,
            blocks=build_expense_approval_blocks(
                {"employee_name": employee_name, "amount": amount, "description": description,
                 "category_code": category_code},
                row_id,
            ),
            text=f"Expense approval request: {description} ({amount})",
        )
        db.set_expense_slack_message(row_id, resp["ts"], resp["channel"])
        receipt_note = f" ({attached} receipt photo(s) attached)" if attached else ""
        notify(f":white_check_mark: Expense submitted for approval (₱{amount:,.2f} — {description}){receipt_note}.")
    except Exception as e:
        logger.error("expense submission failed: %s", e)
        notify(f":x: Failed to submit expense: {e}")


def _report_cash_stats(notify):
    """Shared by `expensetotal` (@mention) and the `query_cash_stats` shortcut."""
    try:
        from datetime import date as _date
        from odoo.expenses import get_month_expense_summary
        today = _date.today()
        summary = get_month_expense_summary(today.year, today.month)
        lines = [
            f":receipt: *{today.strftime('%B %Y')} approved cash expenses* — "
            f"*₱{summary['total']:,.2f}* ({summary['count']} item(s))"
        ]
        for name, amount in sorted(summary["by_employee"].items(), key=lambda x: -x[1]):
            lines.append(f"  • {name}: ₱{amount:,.2f}")

        if Config.PETTY_CASH_ENABLED:
            lines.append("\n*By category:*")
            for name, amount in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
                lines.append(f"  • {name}: ₱{amount:,.2f}")
        lines.append(f"\n:money_with_wings: *Current factory cash balance: ₱{_cash_balance():,.2f}*")

        notify("\n".join(lines))
    except Exception as e:
        logger.error("expensetotal failed: %s", e)
        notify(f":x: Failed to fetch expense total: {e}")


def _report_balance(notify):
    """Shared by `balance` (@mention) — the shortcut path reuses `_report_cash_stats` instead
    since it already includes the balance line."""
    try:
        if Config.PETTY_CASH_ENABLED:
            notify(
                f":money_with_wings: *Factory petty cash balance (Odoo, account {Config.PETTY_CASH_ACCOUNT})*\n"
                f"  • *Current balance: ₱{_cash_balance():,.2f}*\n"
                f"  _Should equal the cash counted in the box._"
            )
            return
        from odoo.expenses import get_all_time_approved_total
        topups = db.get_total_topups()
        spent = get_all_time_approved_total()["total"]
        balance = topups - spent
        notify(
            f":money_with_wings: *Factory cash balance*\n"
            f"  • Total top-ups: ₱{topups:,.2f}\n"
            f"  • Total approved expenses: ₱{spent:,.2f}\n"
            f"  • *Current balance: ₱{balance:,.2f}*"
        )
    except Exception as e:
        logger.error("balance failed: %s", e)
        notify(f":x: Failed to compute balance: {e}")


def _cash_balance() -> float:
    """Factory cash balance: the petty cash account in Odoo, or (petty cash off)
    local top-ups minus approved expenses."""
    from odoo.expenses import get_all_time_approved_total, get_petty_cash_balance
    if Config.PETTY_CASH_ENABLED:
        return get_petty_cash_balance()
    return db.get_total_topups() - get_all_time_approved_total()["total"]


def _record_cash_topup(amount, note, submitted_by, notify):
    """Shared by `cashin` (@mention) and the `cashin_shortcut` modal. With petty
    cash on, top-ups are booked only through the AI assistant (bank -> cash
    entry plus the 支出记录 ledger row), so nothing is recorded here."""
    if Config.PETTY_CASH_ENABLED:
        notify(":information_source: 备用金充值请通过 AI 助手记录（例如：“从银行取现 ₱"
               f"{amount:,.0f} 放备用金”），这里没有记录。/ Record petty cash top-ups with the AI assistant; "
               "nothing was recorded here.")
        return
    db.save_cash_topup(amount, note, submitted_by)
    notify(f":moneybag: Recorded cash top-up of ₱{amount:,.2f}{' — ' + note if note else ''}.")


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


def _update_expense(body: dict, client, action: str, confirm_duplicate: bool = False):
    user_id = body.get("user", {}).get("id", "")
    if user_id not in Config.ADMIN_USER_IDS:
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="⛔ Only an admin can approve or refuse expenses.",
        )
        return

    row_id = int(body["actions"][0]["value"])
    actor = _get_actor(body)
    row = db.get_row("expense_requests", row_id)
    if not row:
        return

    # Petty cash: the expense is booked to its category's account, so the approver
    # must pick one on the card. A confirm from the duplicate prompt has no card
    # state; the category was already written on the first click.
    category_code = ""
    if Config.PETTY_CASH_ENABLED and action == "approved" and not confirm_duplicate:
        category_code = ((((body.get("state") or {}).get("values") or {}).get("expense_category") or {})
                         .get("expense_category_select", {}).get("selected_option") or {}).get("value", "")
        if not category_code:
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="⛔ Choose a category on the card first. / 请先在卡片上选择类别。",
            )
            return

    # Atomically claim the row before touching Odoo — closes the race where a
    # double-click, a redelivered button event, or two admins on the same card
    # could otherwise both pass a check and both call the Odoo action.
    if not db.claim_expense_request(row_id, action):
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text=f"This expense was already {row['status']} — no action taken.",
        )
        return

    from odoo.expenses import (CATEGORY_LABELS, DuplicateExpenseError, approve_expense, refuse_expense,
                               set_expense_category)
    try:
        if action == "approved":
            if category_code:
                set_expense_category(row["odoo_expense_id"], category_code)
            approve_expense(row["odoo_expense_id"], confirm_duplicate=confirm_duplicate)
        else:
            refuse_expense(row["odoo_expense_id"], f"Refused via Slack by {actor}")
    except DuplicateExpenseError as e:
        # Not an error: Odoo wants confirmation. Leave the card pending and ask
        # the approver privately.
        db.update_status("expense_requests", row_id, "pending")
        _ask_duplicate_confirmation(body, client, row, e.duplicates)
        return
    except Exception as e:
        logger.error("Failed to %s expense %s in Odoo: %s", action, row["odoo_expense_id"], e)
        # Odoo call failed after we already claimed the row locally — release
        # the claim so a retry (re-click) is possible instead of being stuck
        # showing "already <status>" for a request Odoo never actually applied.
        db.update_status("expense_requests", row_id, "pending")
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text=f"⛔ Odoo update failed: {e}",
        )
        return

    label = "✅ Approved" if action == "approved" else "❌ Refused"
    if category_code:
        label += f" · {CATEGORY_LABELS.get(category_code, category_code)}"
    new_text = f"~{row['employee_name']} — {row['description']} (₱{row['amount']:,.2f})~ — {label}  _(by {actor})_"
    try:
        # The stored card location also covers clicks from the ephemeral
        # duplicate prompt, whose payload carries no "message".
        client.chat_update(
            channel=row["slack_channel"] or body["channel"]["id"],
            ts=row["slack_ts"] or body["message"]["ts"],
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": new_text}}
            ],
            text=new_text,
        )
    except Exception as e:
        logger.error("Failed to update Slack message: %s", e)

    # An approval just increased all-time spend — warn if it pushed the
    # factory cash balance negative so an admin knows to top it up.
    if action == "approved":
        try:
            balance = _cash_balance()
            if balance < 0:
                client.chat_postMessage(
                    channel=Config.SLACK_CHANNEL_EXPENSE_APPROVAL,
                    text=f":rotating_light: Factory cash balance is now *negative* (₱{balance:,.2f}) "
                         f"after approving this expense. Please record a cash top-up (`cashin`).",
                )
        except Exception as e:
            logger.error("Failed to check cash balance after approval: %s", e)


def _ask_duplicate_confirmation(body: dict, client, row, duplicates: list):
    """Privately ask the approver to confirm an expense Odoo thinks may be a duplicate."""
    lines = "\n".join(
        f"• {d['date']} — {d['name']} (₱{d['total_amount']:,.2f}, {d['state']})" for d in duplicates
    ) or "• (Odoo did not say which one)"
    text = (
        f":warning: *Possible duplicate — not approved yet* / 可能是重复报销，尚未批准\n"
        f"*{row['employee_name']} — {row['description']} (₱{row['amount']:,.2f})* has the same "
        f"employee, date and amount as:\n{lines}\n"
        f"Check the receipts. If it is a different expense, approve it below; "
        f"if it is a duplicate, press Refuse on the original card."
    )
    client.chat_postEphemeral(
        channel=body["channel"]["id"],
        user=body["user"]["id"],
        text=text,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "style": "primary", "action_id": "expense_approve_duplicate",
                 "value": str(row["id"]),
                 "text": {"type": "plain_text", "text": "Not a duplicate — approve / 确认批准"}},
                {"type": "button", "action_id": "expense_duplicate_cancel", "value": str(row["id"]),
                 "text": {"type": "plain_text", "text": "Cancel / 取消"}},
            ]},
        ],
    )
