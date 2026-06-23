"""Block Kit message builders — all text in English."""
from datetime import datetime
from config import Config

ROWS_PER_PAGE = 25  # max customers per Slack message (code block ~3000 char limit)


def _mention(salesperson: str) -> str:
    uid = Config.SALESPERSON_MAP.get(salesperson)
    return f"<@{uid}>" if uid else salesperson or "Unassigned"


def fmt_money(amount: float, currency: str = "₱") -> str:
    return f"{currency}{amount:,.2f}"


# ── Dormant Customer Table ────────────────────────────────────────────────────

def build_customer_table_messages(customers: list) -> list[dict]:
    """Return a list of Slack message payloads (one per page of ROWS_PER_PAGE customers)."""
    total = len(customers)
    pages = [customers[i:i + ROWS_PER_PAGE] for i in range(0, total, ROWS_PER_PAGE)]
    today = datetime.now().strftime("%B %d, %Y")
    messages = []

    for page_num, page in enumerate(pages, 1):
        # Build monospace table
        lines = [
            f"{'#':<4} {'Customer':<26} {'Phone':<16} {'Email':<28} {'Orders':>6}  {'Last Purchase':<14} {'Sales Rep'}",
            "─" * 110,
        ]
        for i, c in enumerate(page, (page_num - 1) * ROWS_PER_PAGE + 1):
            name  = c["name"][:25].ljust(26) if len(c["name"]) > 25 else c["name"].ljust(26)
            phone = (c["phone"] or "—")[:15].ljust(16)
            email = (c["email"] or "—")[:27].ljust(28)
            orders = str(c["order_count"]).rjust(6)
            last  = c["last_order_date"][:13].ljust(14)
            rep   = c["salesperson"] or "—"
            lines.append(f"{i:<4} {name} {phone} {email} {orders}  {last} {rep}")

        table = "```\n" + "\n".join(lines) + "\n```"

        page_label = f"Page {page_num}/{len(pages)}" if len(pages) > 1 else ""
        header_text = (
            f"*📋 Dormant Customer Follow-up List — {total} customers  |  {today}*"
            + (f"  _{page_label}_" if page_label else "")
        )

        messages.append({
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": header_text},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": table},
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"Customers with no purchase in the last *{Config.DORMANT_CUSTOMER_DAYS} days*. Reply in thread to log your follow-up."}
                    ],
                },
            ],
            "text": f"Dormant Customer List — {total} customers ({today})",
        })

    return messages


# ── Dormant Customer Card (individual) ────────────────────────────────────────

def build_customer_blocks(customer: dict, db_row_id: int) -> list:
    days = customer["days_inactive"]
    last_date = customer["last_order_date"]
    amount = customer["last_order_amount"]

    last_purchase = f"{last_date} ({days} days ago)" if last_date != "Never purchased" else "Never purchased"
    total_amount = fmt_money(amount) if amount else "—"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📋 Dormant Customer Follow-up", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Customer*\n{customer['name']}"},
                {"type": "mrkdwn", "text": f"*Sales Rep*\n{_mention(customer['salesperson'])}"},
                {"type": "mrkdwn", "text": f"*Last Purchase*\n{last_purchase}"},
                {"type": "mrkdwn", "text": f"*Total Purchase History*\n{total_amount}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Contacted", "emoji": True},
                    "style": "primary",
                    "action_id": "customer_contacted",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📅 Schedule Follow-up", "emoji": True},
                    "action_id": "customer_scheduled",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭ Skip", "emoji": True},
                    "action_id": "customer_skipped",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Won't Buy Again", "emoji": True},
                    "style": "danger",
                    "action_id": "customer_excluded",
                    "value": str(db_row_id),
                },
            ],
        },
        {"type": "divider"},
    ]


# ── Overdue Invoice ───────────────────────────────────────────────────────────

def build_invoice_blocks(invoice: dict, db_row_id: int) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "💰 Overdue Invoice Reminder", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Customer*\n{invoice['partner_name']}"},
                {"type": "mrkdwn", "text": f"*Assigned To*\n{_mention(invoice['salesperson'])}"},
                {"type": "mrkdwn", "text": f"*Invoice No.*\n{invoice['invoice_name']}"},
                {"type": "mrkdwn", "text": f"*Due Date*\n{invoice['due_date']} ({invoice['days_overdue']} days overdue)"},
                {"type": "mrkdwn", "text": f"*Amount Due*\n*{fmt_money(invoice['amount_residual'], invoice['currency'])}*"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Followed Up", "emoji": True},
                    "style": "primary",
                    "action_id": "invoice_followed",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📞 Log Call", "emoji": True},
                    "action_id": "invoice_called",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭ Skip", "emoji": True},
                    "style": "danger",
                    "action_id": "invoice_skipped",
                    "value": str(db_row_id),
                },
            ],
        },
        {"type": "divider"},
    ]


# ── Quotation Follow-up ───────────────────────────────────────────────────────

def build_quotation_blocks(quote: dict, db_row_id: int) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📝 Quotation Follow-up Reminder", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Customer*\n{quote['partner_name']}"},
                {"type": "mrkdwn", "text": f"*Sales Rep*\n{_mention(quote['salesperson'])}"},
                {"type": "mrkdwn", "text": f"*Quotation No.*\n{quote['order_name']}"},
                {"type": "mrkdwn", "text": f"*Sent On*\n{quote['create_date']} ({quote['days_pending']} days, no response)"},
                {"type": "mrkdwn", "text": f"*Amount*\n{fmt_money(quote['amount_total'], quote['currency'])}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Followed Up", "emoji": True},
                    "style": "primary",
                    "action_id": "quote_followed",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Close Quotation", "emoji": True},
                    "style": "danger",
                    "action_id": "quote_closed",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭ Skip", "emoji": True},
                    "action_id": "quote_skipped",
                    "value": str(db_row_id),
                },
            ],
        },
        {"type": "divider"},
    ]


# ── Low Stock ─────────────────────────────────────────────────────────────────

def build_stock_blocks(product: dict, db_row_id: int) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️ Low Stock Alert", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Product*\n{product['product_name']}"},
                {"type": "mrkdwn", "text": f"*Location*\n{product['warehouse']}"},
                {"type": "mrkdwn", "text": f"*On Hand*\n*{product['qty_on_hand']}* units"},
                {"type": "mrkdwn", "text": f"*Reorder Point*\n{product['reorder_point']} units"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Handled", "emoji": True},
                    "style": "primary",
                    "action_id": "stock_handled",
                    "value": str(db_row_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭ Ignore", "emoji": True},
                    "style": "danger",
                    "action_id": "stock_ignored",
                    "value": str(db_row_id),
                },
            ],
        },
        {"type": "divider"},
    ]


# ── Google Sheet notification ─────────────────────────────────────────────────

def build_sheet_notification_blocks(total: int, sheet_url: str) -> list:
    today = datetime.now().strftime("%B %d, %Y")
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📋 Dormant Customer Follow-up List Updated", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{total} customers* haven't purchased in the last *{Config.DORMANT_CUSTOMER_DAYS} days*.\n"
                    f"The follow-up list has been updated in Google Sheets — {today}.\n\n"
                    f"Please open the sheet, find your assigned customers, and update the *Status* column after follow-up."
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "📊 Open Google Sheet", "emoji": True},
                "style": "primary",
                "url": sheet_url,
                "action_id": "open_sheet",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Status options: *Pending* → *Contacted* → *Reactivated* or *Won't Buy*"}
            ],
        },
        {"type": "divider"},
    ]


def build_overdue_invoice_sheet_notification_blocks(total: int, sheet_url: str) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "💰 Overdue Invoice Follow-up List Updated", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{total} invoice(s)* are overdue (within the last {Config.OVERDUE_INVOICE_MAX_DAYS} days).\n"
                    f"The follow-up list has been updated in Google Sheets.\n\n"
                    f"Please open the sheet, find your assigned customers, follow up on payment, and update the "
                    f"*Status* column."
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "📊 Open Google Sheet", "emoji": True},
                "style": "primary",
                "url": sheet_url,
                "action_id": "open_sheet",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Status options: *Pending* → *Contacted* → *Payment Promised* → *Paid* / *Disputed*"}
            ],
        },
        {"type": "divider"},
    ]


def build_legacy_invoice_notification_blocks(total: int, sheet_url: str) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🗂 Legacy Overdue Invoices — Manual Review Needed", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{total} invoice(s)* are overdue by more than *{Config.OVERDUE_INVOICE_MAX_DAYS} days* "
                    f"and no longer appear in the daily reminder.\n"
                    f"Many of these are likely already paid (recording mistakes), but each one needs to be "
                    f"checked manually before we touch the Odoo records.\n\n"
                    f"Please open the sheet, verify each invoice against actual payment records, and set its "
                    f"*Status* column accordingly."
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "📊 Open Google Sheet", "emoji": True},
                "style": "primary",
                "url": sheet_url,
                "action_id": "open_sheet",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Status options: *Pending Review* → *Confirmed Paid* / *Confirmed Unpaid - Follow Up* / *Write Off*"}
            ],
        },
        {"type": "divider"},
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _progress_bar(pct: float, width: int = 12) -> str:
    filled = min(int(width * pct / 100), width)
    return "█" * filled + "░" * (width - filled)


def _rep_ranking(by_person: list) -> str:
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, data) in enumerate(by_person):
        medal = medals[i] if i < 3 else "  •"
        lines.append(f"{medal} {name}  ₱{data['amount']:,.0f}  ({data['orders']} orders)")
    return "\n".join(lines) or "No data"


# ── Daily Report ──────────────────────────────────────────────────────────────

def build_daily_report_blocks(data: dict, target: float = 0) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Daily Sales Report — {data['date']}", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Orders Today*\n{data['order_count']}"},
                {"type": "mrkdwn", "text": f"*Sales Today*\n₱{data['total_sales']:,.2f}"},
                {"type": "mrkdwn", "text": f"*Collected Today*\n₱{data['total_collected']:,.2f}"},
                {"type": "mrkdwn", "text": f"*New Customers*\n{data['new_customers']}"},
            ],
        },
    ]

    # Monthly target progress (only if target is set)
    if target > 0:
        pct = min(data["month_sales"] / target * 100, 100)
        bar = _progress_bar(pct)
        days_left = data["days_left"]
        daily_needed = max((target - data["month_sales"]) / days_left, 0) if days_left > 0 else 0
        target_text = (
            f"*Monthly Target Progress*\n"
            f"`{bar}` {pct:.1f}%\n"
            f"₱{data['month_sales']:,.0f} / ₱{target:,.0f}  |  "
            f"{days_left} days left  |  Need ₱{daily_needed:,.0f}/day"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": target_text}})

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Sales Rep Ranking*\n{_rep_ranking(data['by_person'])}"},
    })
    blocks.append({"type": "divider"})
    return blocks


# ── Weekly Report ─────────────────────────────────────────────────────────────

def build_weekly_report_blocks(data: dict) -> list:
    customer_lines = "\n".join(
        f"{i+1}. {c['name']}  ₱{c['amount']:,.0f}  ({c['orders']} orders)"
        for i, c in enumerate(data["top_customers"])
    ) or "No data"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📈 Weekly Sales Report — {data['week_label']}", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Orders*\n{data['order_count']}"},
                {"type": "mrkdwn", "text": f"*Total Sales*\n₱{data['total_sales']:,.2f}"},
                {"type": "mrkdwn", "text": f"*Total Collected*\n₱{data['total_collected']:,.2f}"},
                {"type": "mrkdwn", "text": f"*New Customers*\n{data['new_customers']}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Sales Rep Ranking*\n{_rep_ranking(data['by_person'])}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Top 5 Customers*\n{customer_lines}"},
        },
        {"type": "divider"},
    ]


# ── Monthly Report ───────────────────────────────────────────────────────────

def build_monthly_report_blocks(data: dict, target: float = 0) -> list:
    customer_lines = "\n".join(
        f"{i+1}. {c['name']}  ₱{c['amount']:,.0f}  ({c['orders']} orders)"
        for i, c in enumerate(data["top_customers"])
    ) or "No data"

    collection_rate = (data["total_collected"] / data["total_invoiced"] * 100) \
        if data["total_invoiced"] > 0 else 0

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📅 Monthly Sales Report — {data['month_label']}", "emoji": True},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Total Orders*\n{data['order_count']}"},
                {"type": "mrkdwn", "text": f"*Total Sales*\n₱{data['total_sales']:,.2f}"},
                {"type": "mrkdwn", "text": f"*Total Invoiced*\n₱{data['total_invoiced']:,.2f}"},
                {"type": "mrkdwn", "text": f"*Total Collected*\n₱{data['total_collected']:,.2f}"},
                {"type": "mrkdwn", "text": f"*Collection Rate*\n{collection_rate:.1f}%"},
                {"type": "mrkdwn", "text": f"*New Customers*\n{data['new_customers']}"},
            ],
        },
    ]

    # Target progress
    if target > 0:
        pct = min(data["total_sales"] / target * 100, 100)
        bar = _progress_bar(pct)
        gap = data["total_sales"] - target
        gap_text = f"✅ Exceeded by ₱{gap:,.0f}" if gap >= 0 else f"⚠️ Short by ₱{abs(gap):,.0f}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            f"*Monthly Target*\n"
            f"`{bar}` {pct:.1f}%  —  {gap_text}\n"
            f"₱{data['total_sales']:,.0f} / ₱{target:,.0f}"
        )}})

    blocks += [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Sales Rep Ranking*\n{_rep_ranking(data['by_person'])}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Top 10 Customers*\n{customer_lines}"},
        },
        {"type": "divider"},
    ]
    return blocks


# ── New Order Notification ────────────────────────────────────────────────────

def build_new_order_blocks(order: dict) -> list:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🛒 *New Order Confirmed*\n"
                    f"*Customer:* {order['partner']}   *Order:* {order['order_name']}\n"
                    f"*Amount:* {order['currency']}{order['amount']:,.2f}   "
                    f"*Sales Rep:* {_mention(order['salesperson'])}\n"
                    f"*Warehouse:* {order['warehouse']}   "
                    f"*Delivery Status:* {order['delivery_status']}"
                ),
            },
        },
    ]


# ── Payment Received Notification ─────────────────────────────────────────────

def build_payment_blocks(inv: dict) -> list:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"💵 *Payment Received — Fully Paid* ✅\n"
                    f"*Customer:* {inv['partner']}   *Invoice:* {inv['invoice_name']}\n"
                    f"*Amount:* {inv['currency']}{inv['amount']:,.2f}   "
                    f"*Sales Rep:* {_mention(inv['salesperson'])}\n"
                    f"*Payment Method:* {inv['payment_method']}   "
                    f"*Account:* {inv['account']}\n"
                    f"*Pay Date:* {inv.get('pay_date', '—')}   "
                    f"*Order Date:* {inv.get('order_date', '—')}\n"
                    f"*Order:* {inv['order_name']}   *Warehouse:* {inv['warehouse']}"
                ),
            },
        },
    ]


# ── Customer Reactivation Alert ───────────────────────────────────────────────

def build_reactivation_blocks(customer: dict) -> list:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🎉 *Customer Reactivated!*\n"
                    f"*{customer['partner_name']}* (previously marked Won't Buy) just placed a new order!\n"
                    f"*Order:* {customer['order_name']}   "
                    f"*Amount:* {customer['currency']}{customer['amount']:,.2f}   "
                    f"*Date:* {customer['date']}"
                ),
            },
        },
    ]


# ── Data Integrity Alert ──────────────────────────────────────────────────────

def build_integrity_alert_text(issues: list[dict]) -> str:
    by_type: dict[str, list[str]] = {}
    for issue in issues:
        by_type.setdefault(issue["type"], []).append(issue["detail"])

    lines = [f"🚨 *Odoo Data Integrity Check — {len(issues)} issue(s) found*"]
    for issue_type, details in by_type.items():
        lines.append(f"\n*{issue_type}* ({len(details)})")
        for d in details:
            lines.append(f"• {d}")
    return "\n".join(lines)


def build_new_customer_missing_contact_text(customers: list[dict]) -> str:
    lines = [f"📇 *New customer(s) missing contact info — {len(customers)}*"]
    for c in customers:
        lines.append(f"• {c['name']} — missing: {', '.join(c['missing'])}")
    return "\n".join(lines)
