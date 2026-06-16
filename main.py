"""Entry point — starts scheduler + Slack socket connection."""
import logging
import sys
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import Config
import db
from odoo.client import get_client
from slack.app import get_socket_handler
from scheduler.jobs import (
    job_dormant_customers,
    job_sync_wont_buy,
    job_overdue_invoices,
    job_export_overdue_invoices,
    job_export_legacy_invoices,
    job_pending_quotations,
    job_low_stock,
    job_daily_report,
    job_weekly_report,
    job_monthly_report,
    job_new_orders,
    job_new_payments,
    job_reactivation_check,
    job_integrity_check,
    job_new_customer_contact_check,
)
from odoo.orders import get_max_order_id
from odoo.payments import now_utc_str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
tz = pytz.timezone(Config.TIMEZONE)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=tz)

    # Every 5 min — poll for new confirmed orders
    scheduler.add_job(job_new_orders, "interval", minutes=5, id="new_orders")

    # Every 5 min — poll for new payments
    scheduler.add_job(job_new_payments, "interval", minutes=5, id="new_payments")

    # Every 5 min — poll for new customers missing contact info (phone/email/Facebook/Viber)
    scheduler.add_job(job_new_customer_contact_check, "interval", minutes=5, id="new_customer_contact_check")

    # Every 4 hours — check if excluded customers have reactivated
    scheduler.add_job(job_reactivation_check, "interval", hours=4, id="reactivation_check")

    # Every 2 hours — sync Won't Buy from Google Sheet to blacklist
    scheduler.add_job(job_sync_wont_buy, "interval", hours=2, id="sync_wont_buy")

    # Every 1 hour — data integrity check (zero/negative invoices, date logic
    # errors, payment_state mismatches, possible duplicate invoices, orders
    # newly stale beyond 30 days uninvoiced)
    scheduler.add_job(job_integrity_check, "interval", hours=1, id="integrity_check")

    # 08:00 — low stock alert (warehouse needs early warning)
    scheduler.add_job(job_low_stock, CronTrigger(hour=8, minute=0, timezone=tz), id="low_stock")

    # 09:00 — dormant customer reminders
    scheduler.add_job(job_dormant_customers, CronTrigger(hour=9, minute=0, timezone=tz), id="dormant_customers")

    # 09:30 — overdue invoices
    scheduler.add_job(job_overdue_invoices, CronTrigger(hour=9, minute=30, timezone=tz), id="overdue_invoices")

    # 09:35 — overdue invoices follow-up sheet (employees track progress)
    scheduler.add_job(job_export_overdue_invoices, CronTrigger(hour=9, minute=35, timezone=tz), id="export_overdue_invoices")

    # 10:00 — pending quotations
    scheduler.add_job(job_pending_quotations, CronTrigger(hour=10, minute=0, timezone=tz), id="pending_quotations")

    # 18:00 — daily sales report (end of business day)
    scheduler.add_job(job_daily_report, CronTrigger(hour=18, minute=0, timezone=tz), id="daily_report")

    # Monday 08:30 — weekly report for last week
    scheduler.add_job(job_weekly_report, CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=tz), id="weekly_report")

    # Monday 09:45 — refresh legacy overdue invoice review sheet (>30 days, not daily noise)
    scheduler.add_job(job_export_legacy_invoices, CronTrigger(day_of_week="mon", hour=9, minute=45, timezone=tz), id="legacy_invoices")

    # 1st of each month 09:00 — monthly report for last month
    scheduler.add_job(job_monthly_report, CronTrigger(day=1, hour=9, minute=0, timezone=tz), id="monthly_report")

    return scheduler


def main():
    logger.info("Starting Odoo-Slack Bot...")

    # Verify Odoo connection
    logger.info("Connecting to Odoo: %s", Config.ODOO_URL)
    client = get_client()
    if not client.ping():
        logger.error("Cannot connect to Odoo. Check .env settings.")
        sys.exit(1)
    logger.info("Odoo connection OK (uid=%s)", client.uid)

    # Initialize local database
    db.init_db()
    logger.info("Local state DB initialized.")

    # Initialize polling cursors (only on first run)
    if not db.get_state("last_order_id"):
        max_id = get_max_order_id()
        db.set_state("last_order_id", str(max_id))
        logger.info("Initialized order cursor at id=%d", max_id)
    if not db.get_state("last_payment_ts"):
        db.set_state("last_payment_ts", now_utc_str())
        logger.info("Initialized payment cursor at now")

    # Start scheduler
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("Scheduler started. Jobs:")
    for job in scheduler.get_jobs():
        logger.info("  %s → next run: %s", job.id, job.next_run_time)

    # Start Slack Socket Mode (blocks until Ctrl+C)
    logger.info("Connecting to Slack via Socket Mode...")
    handler = get_socket_handler()
    handler.start()


if __name__ == "__main__":
    main()
