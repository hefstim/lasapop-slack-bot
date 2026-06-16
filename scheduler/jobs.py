"""APScheduler job definitions — one function per scheduled task."""
import logging
import time
from datetime import date
from slack_sdk import WebClient
from config import Config

# Max cards per run to avoid flooding the channel
MAX_CARDS_PER_RUN = 20
import db
from odoo.customers import get_dormant_customers, get_new_customers_missing_contact
from odoo.invoices import get_overdue_invoices, get_legacy_overdue_invoices
from odoo.quotations import get_pending_quotations
from odoo.inventory import get_low_stock_products
from odoo.reports import get_daily_report, get_weekly_report, get_monthly_report
from odoo.orders import get_new_confirmed_orders, get_max_order_id
from odoo.payments import get_new_payments, get_reactivated_customers, now_utc_str
from odoo.integrity import check_invoice_anomalies, check_newly_stale_orders
from slack.messages import (
    build_customer_table_messages,
    build_customer_blocks,
    build_sheet_notification_blocks,
    build_overdue_invoice_sheet_notification_blocks,
    build_legacy_invoice_notification_blocks,
    build_invoice_blocks,
    build_quotation_blocks,
    build_stock_blocks,
    build_daily_report_blocks,
    build_weekly_report_blocks,
    build_monthly_report_blocks,
    build_new_order_blocks,
    build_payment_blocks,
    build_reactivation_blocks,
    build_integrity_alert_text,
    build_new_customer_missing_contact_text,
)
from google_sheets.exporter import export_dormant_customers, sync_wont_buy_to_blacklist
from google_sheets.invoice_exporter import export_overdue_invoices, export_legacy_invoices

logger = logging.getLogger(__name__)


def _slack() -> WebClient:
    return WebClient(token=Config.SLACK_BOT_TOKEN)


def _post(slack: WebClient, retries: int = 3, **kwargs):
    """Post a message with retry on rate limit."""
    for attempt in range(retries):
        try:
            return slack.chat_postMessage(**kwargs)
        except Exception as e:
            if "ratelimited" in str(e) and attempt < retries - 1:
                wait = 60 * (attempt + 1)
                logger.warning("Rate limited. Waiting %ds...", wait)
                time.sleep(wait)
            else:
                raise


# ── Job: dormant customers ────────────────────────────────────────────────────

def job_dormant_customers():
    logger.info("Running: dormant customers check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_SALES

    try:
        customers = get_dormant_customers()
    except Exception as e:
        logger.error("Odoo query failed (customers): %s", e)
        return

    if not customers:
        logger.info("No dormant customers today.")
        return

    # Export to Google Sheets and post a single link message
    try:
        sheet_url = export_dormant_customers(customers)
        _post(
            slack,
            channel=channel,
            blocks=build_sheet_notification_blocks(len(customers), sheet_url),
            text=f"Dormant customer list updated — {len(customers)} customers. Open Google Sheet to follow up.",
        )
        logger.info("Exported %d dormant customers to Google Sheets.", len(customers))
    except Exception as e:
        logger.error("Failed to export to Google Sheets: %s", e)


# ── Job: sync Won't Buy from Google Sheet to blacklist ───────────────────────

def job_sync_wont_buy():
    logger.info("Running: sync Won't Buy from Google Sheet")
    try:
        added = sync_wont_buy_to_blacklist()
        if added:
            logger.info("Blacklisted %d new customer(s) from Google Sheet.", added)
        else:
            logger.info("No new Won't Buy entries to sync.")
    except Exception as e:
        logger.error("Failed to sync Won't Buy: %s", e)


# ── Job: overdue invoices ─────────────────────────────────────────────────────

def job_overdue_invoices():
    logger.info("Running: overdue invoices check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_FINANCE

    try:
        invoices = get_overdue_invoices()
    except Exception as e:
        logger.error("Odoo query failed (invoices): %s", e)
        return

    if not invoices:
        logger.info("No overdue invoices today.")
        return

    _post(slack,
        channel=channel,
        text=f"💰 *Overdue Invoice Alert — {len(invoices)} invoice(s) need follow-up. Please collect payment!*",
    )

    sent = 0
    for inv in invoices:
        if sent >= MAX_CARDS_PER_RUN:
            break
        if db.already_reminded_today("invoice_reminders", "invoice_id", inv["invoice_id"]):
            continue
        try:
            resp = _post(slack,
                channel=channel,
                blocks=build_invoice_blocks(inv, 0),
                text=f"逾期发票：{inv['invoice_name']} — {inv['partner_name']}",
            )
            ts = resp["ts"]
            channel_id = resp["channel"]
            row_id = db.save_invoice_reminder(
                inv["invoice_id"], inv["invoice_name"], inv["partner_name"],
                inv["amount_residual"], inv["due_date"], inv["days_overdue"], ts, channel_id,
            )
            slack.chat_update(
                channel=channel_id,
                ts=ts,
                blocks=build_invoice_blocks(inv, row_id),
                text=f"Overdue invoice: {inv['invoice_name']}",
            )
            sent += 1
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post invoice reminder for %s: %s", inv["invoice_name"], e)


# ── Job: overdue invoices export (active follow-up, ≤30 days) ────────────────

def job_export_overdue_invoices():
    logger.info("Running: overdue invoices export to Google Sheet")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_FINANCE

    try:
        invoices = get_overdue_invoices()
    except Exception as e:
        logger.error("Odoo query failed (overdue invoices export): %s", e)
        return

    if not invoices:
        logger.info("No overdue invoices to export.")
        return

    try:
        sheet_url = export_overdue_invoices(invoices)
        _post(
            slack,
            channel=channel,
            blocks=build_overdue_invoice_sheet_notification_blocks(len(invoices), sheet_url),
            text=f"Overdue invoice list updated — {len(invoices)} invoice(s) need follow-up.",
        )
        logger.info("Exported %d overdue invoices to Google Sheets.", len(invoices))
    except Exception as e:
        logger.error("Failed to export overdue invoices to Google Sheets: %s", e)


# ── Job: legacy overdue invoices (manual review export) ──────────────────────

def job_export_legacy_invoices():
    logger.info("Running: legacy overdue invoices export")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_FINANCE

    try:
        invoices = get_legacy_overdue_invoices()
    except Exception as e:
        logger.error("Odoo query failed (legacy invoices): %s", e)
        return

    if not invoices:
        logger.info("No legacy overdue invoices to review.")
        return

    try:
        sheet_url = export_legacy_invoices(invoices)
        _post(
            slack,
            channel=channel,
            blocks=build_legacy_invoice_notification_blocks(len(invoices), sheet_url),
            text=f"Legacy overdue invoice list updated — {len(invoices)} invoice(s) need manual review.",
        )
        logger.info("Exported %d legacy overdue invoices to Google Sheets.", len(invoices))
    except Exception as e:
        logger.error("Failed to export legacy invoices to Google Sheets: %s", e)


# ── Job: pending quotations ───────────────────────────────────────────────────

def job_pending_quotations():
    logger.info("Running: pending quotations check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_SALES

    try:
        quotes = get_pending_quotations()
    except Exception as e:
        logger.error("Odoo query failed (quotations): %s", e)
        return

    if not quotes:
        logger.info("No stale quotations today.")
        return

    _post(slack,
        channel=channel,
        text=f"📝 *Quotation Follow-up — {len(quotes)} quotation(s) with no response. Please follow up!*",
    )

    sent = 0
    for quote in quotes:
        if sent >= MAX_CARDS_PER_RUN:
            break
        if db.already_reminded_today("quotation_reminders", "order_id", quote["order_id"]):
            continue
        try:
            resp = _post(slack,
                channel=channel,
                blocks=build_quotation_blocks(quote, 0),
                text=f"待跟进报价：{quote['order_name']} — {quote['partner_name']}",
            )
            ts = resp["ts"]
            channel_id = resp["channel"]
            row_id = db.save_quotation_reminder(
                quote["order_id"], quote["order_name"], quote["partner_name"],
                quote["amount_total"], quote["create_date"], quote["days_pending"],
                quote["salesperson"], ts, channel_id,
            )
            slack.chat_update(
                channel=channel_id,
                ts=ts,
                blocks=build_quotation_blocks(quote, row_id),
                text=f"Pending quotation: {quote['order_name']}",
            )
            sent += 1
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post quotation reminder for %s: %s", quote["order_name"], e)


# ── Job: low stock ────────────────────────────────────────────────────────────

def job_low_stock():
    logger.info("Running: low stock check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_WAREHOUSE

    try:
        products = get_low_stock_products()
    except Exception as e:
        logger.error("Odoo query failed (inventory): %s", e)
        return

    if not products:
        logger.info("No low stock alerts today.")
        return

    _post(slack,
        channel=channel,
        text=f"⚠️ *Low Stock Alert — {len(products)} product(s) below reorder point. Please replenish!*",
    )

    sent = 0
    for product in products:
        if sent >= MAX_CARDS_PER_RUN:
            break
        if db.already_reminded_today("stock_alerts", "product_id", product["product_id"]):
            continue
        try:
            resp = _post(slack,
                channel=channel,
                blocks=build_stock_blocks(product, 0),
                text=f"库存不足：{product['product_name']}（剩余 {product['qty_on_hand']}）",
            )
            ts = resp["ts"]
            channel_id = resp["channel"]
            row_id = db.save_stock_alert(
                product["product_id"], product["product_name"],
                product["qty_on_hand"], product["reorder_point"], ts, channel_id,
            )
            slack.chat_update(
                channel=channel_id,
                ts=ts,
                blocks=build_stock_blocks(product, row_id),
                text=f"Low stock: {product['product_name']}",
            )
            sent += 1
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post stock alert for %s: %s", product["product_name"], e)


# ── Job: new order notification (polling) ─────────────────────────────────────

def job_new_orders():
    slack = _slack()
    channel = Config.SLACK_CHANNEL_SALES
    last_id = int(db.get_state("last_order_id", "0"))

    try:
        orders = get_new_confirmed_orders(last_id)
    except Exception as e:
        logger.error("Odoo query failed (new orders): %s", e)
        return

    max_id = last_id
    for order in orders:
        try:
            _post(slack, channel=channel,
                  blocks=build_new_order_blocks(order),
                  text=f"New order: {order['order_name']} — {order['partner']}")
            max_id = max(max_id, order["order_id"])
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post new order %s: %s", order["order_name"], e)

    if max_id != last_id:
        db.set_state("last_order_id", str(max_id))


# ── Job: payment received notification (polling) ──────────────────────────────

def job_new_payments():
    slack = _slack()
    channel = Config.SLACK_CHANNEL_FINANCE
    since_ts = db.get_state("last_payment_ts", now_utc_str())

    try:
        payments = get_new_payments(since_ts)
    except Exception as e:
        logger.error("Odoo query failed (payments): %s", e)
        return

    new_ts = since_ts
    for inv in payments:
        try:
            _post(slack, channel=channel,
                  blocks=build_payment_blocks(inv),
                  text=f"Payment received: {inv['invoice_name']} — {inv['partner']}")
            if inv["write_date"] > new_ts:
                new_ts = inv["write_date"]
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post payment %s: %s", inv["invoice_name"], e)

    db.set_state("last_payment_ts", new_ts)


# ── Job: reactivation alert ───────────────────────────────────────────────────

def job_reactivation_check():
    logger.info("Running: reactivation check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_MANAGEMENT

    excluded = db.get_all_excluded_customers()
    try:
        reactivated = get_reactivated_customers(excluded)
    except Exception as e:
        logger.error("Odoo query failed (reactivation): %s", e)
        return

    for c in reactivated:
        try:
            _post(slack, channel=channel,
                  blocks=build_reactivation_blocks(c),
                  text=f"Customer reactivated: {c['partner_name']}")
            db.remove_excluded_customer(c["partner_id"])
            logger.info("Removed %s from blacklist (reactivated).", c["partner_name"])
            time.sleep(1.2)
        except Exception as e:
            logger.error("Failed to post reactivation for %s: %s", c["partner_name"], e)


# ── Job: daily report ─────────────────────────────────────────────────────────

def job_daily_report():
    logger.info("Running: daily sales report")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_MANAGEMENT

    try:
        data = get_daily_report()
    except Exception as e:
        logger.error("Odoo query failed (daily report): %s", e)
        return

    try:
        _post(slack, channel=channel,
              blocks=build_daily_report_blocks(data, target=Config.MONTHLY_SALES_TARGET),
              text=f"Daily Sales Report — {data['date']}")
    except Exception as e:
        logger.error("Failed to post daily report: %s", e)


# ── Job: monthly report ───────────────────────────────────────────────────────

def job_monthly_report():
    logger.info("Running: monthly sales report")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_MANAGEMENT
    # Report for last month (this job runs on 1st of each month)
    today = date.today()
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1
    try:
        data = get_monthly_report(year=y, month=m)
    except Exception as e:
        logger.error("Odoo query failed (monthly report): %s", e)
        return
    try:
        _post(slack, channel=channel,
              blocks=build_monthly_report_blocks(data, target=Config.MONTHLY_SALES_TARGET),
              text=f"Monthly Sales Report — {data['month_label']}")
    except Exception as e:
        logger.error("Failed to post monthly report: %s", e)


# ── Job: current month report (on-demand) ────────────────────────────────────

def job_current_month_report():
    logger.info("Running: current month report")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_MANAGEMENT
    today = date.today()
    try:
        data = get_monthly_report(year=today.year, month=today.month)
    except Exception as e:
        logger.error("Odoo query failed (current month report): %s", e)
        return
    try:
        _post(slack, channel=channel,
              blocks=build_monthly_report_blocks(data, target=Config.MONTHLY_SALES_TARGET),
              text=f"Current Month Report — {data['month_label']}")
    except Exception as e:
        logger.error("Failed to post current month report: %s", e)


# ── Job: weekly report ────────────────────────────────────────────────────────

def job_weekly_report():
    logger.info("Running: weekly sales report")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_MANAGEMENT

    try:
        data = get_weekly_report()
    except Exception as e:
        logger.error("Odoo query failed (weekly report): %s", e)
        return

    try:
        _post(slack, channel=channel,
              blocks=build_weekly_report_blocks(data),
              text=f"Weekly Sales Report — {data['week_label']}")
    except Exception as e:
        logger.error("Failed to post weekly report: %s", e)


# ── Job: data integrity check ─────────────────────────────────────────────────

def job_integrity_check():
    logger.info("Running: data integrity check")
    slack = _slack()
    channel = Config.SLACK_CHANNEL_ODOO_REPORT
    now = now_utc_str()
    since_ts = db.get_state("integrity_last_check_ts", now)

    issues = []
    try:
        issues += check_invoice_anomalies(since_ts)
    except Exception as e:
        logger.error("Odoo query failed (integrity invoices): %s", e)

    order_issues = []
    try:
        order_issues = check_newly_stale_orders(db.get_alerted_order_ids())
        issues += order_issues
    except Exception as e:
        logger.error("Odoo query failed (integrity orders): %s", e)

    if issues:
        try:
            _post(slack, channel=channel, text=build_integrity_alert_text(issues))
        except Exception as e:
            logger.error("Failed to post integrity alert: %s", e)
        else:
            if order_issues:
                db.mark_orders_alerted([i["order_id"] for i in order_issues])

    db.set_state("integrity_last_check_ts", now)


# ── Job: new customer missing contact info ────────────────────────────────────

def job_new_customer_contact_check():
    slack = _slack()
    channel = Config.SLACK_CHANNEL_ODOO_REPORT
    now = now_utc_str()
    since_ts = db.get_state("last_customer_check_ts", now)

    try:
        customers = get_new_customers_missing_contact(since_ts)
    except Exception as e:
        logger.error("Odoo query failed (new customer contact check): %s", e)
        return

    if customers:
        try:
            _post(slack, channel=channel, text=build_new_customer_missing_contact_text(customers))
        except Exception as e:
            logger.error("Failed to post new customer contact alert: %s", e)

    db.set_state("last_customer_check_ts", now)
