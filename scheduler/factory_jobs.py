"""LasaPop factory scheduled jobs (registered in main.py only when FACTORY_ENABLED)."""
import logging
from datetime import date

from slack_sdk import WebClient

from config import Config
from odoo import factory

logger = logging.getLogger(__name__)
ALERT_DAYS = 30


def job_expiring_lots():
    """08:00 — lots in stock that expire within ALERT_DAYS (or already expired) → warehouse channel."""
    try:
        rows = factory.expiring_lots(ALERT_DAYS)
    except Exception as e:
        logger.error("job_expiring_lots failed: %s", e)
        return
    if not rows:
        return
    today = date.today().isoformat()
    expired = [q for q in rows if q["expiry"] < today]
    soon = [q for q in rows if q["expiry"] >= today]
    line = lambda q: f"• `{q['lot']}` {q['product']} · {q['location']} · {q['qty']:g} {q['uom']} · 到期 {q['expiry']}"
    parts = ["⏰ *临期提醒 / Expiry alert*"]
    if expired:
        parts.append("⚠️ *已过期（请报废或处理）*\n" + "\n".join(line(q) for q in expired))
    if soon:
        parts.append(f"🔔 *{ALERT_DAYS} 天内到期（先用这些）*\n" + "\n".join(line(q) for q in soon))
    WebClient(token=Config.SLACK_BOT_TOKEN).chat_postMessage(channel=Config.SLACK_CHANNEL_WAREHOUSE,
                                                             text="\n\n".join(parts)[:3900])
