"""
现金支出 & 线上收款相关定时任务。
"""
import logging
from datetime import date, datetime, timezone, timedelta

from slack_sdk import WebClient
from config import Config

logger = logging.getLogger(__name__)


def _slack() -> WebClient:
    return WebClient(token=Config.SLACK_BOT_TOKEN)


# ── 每晚 21:00：现金结余核对（Odoo 收款 vs Sheet 记录）─────────────────────────

def job_cash_balance_verify():
    """
    双重核对：
    1. Sheet 自查：期初 + 收入 - 支出 ± 调拨 = 结余（检查 Sheet 记录算术是否正确）
    2. Odoo vs Sheet 收入对比：Odoo CSH1 现金收款应等于 Sheet 记录的现金收入
       Odoo > Sheet → 员工可能把银行转账错填为现金
       Odoo < Sheet → 员工可能在 Sheet 虚报了现金收入
    """
    from google_sheets.cash_reader import (
        get_daily_cashflow,
        get_closing_balance,
        get_previous_closing_balance,
        BRANCH_TABS,
    )
    from odoo.cash_income import get_cash_income_by_branch

    today = date.today()
    branch_display = {"qc": "QC", "antipolo": "Antipolo", "bulacan": "Bulacan"}

    lines = [f"💰 *Cash Verification — {today.strftime('%Y-%m-%d')}*\n"]
    any_issue = False

    try:
        odoo_income = get_cash_income_by_branch(today)
    except Exception as e:
        logger.error("job_cash_balance_verify: Odoo query failed: %s", e)
        odoo_income = {}

    for branch in BRANCH_TABS:
        bname     = branch_display.get(branch, branch.upper())
        today_bal = get_closing_balance(branch, today)
        prev_bal  = get_previous_closing_balance(branch, today)
        cf        = get_daily_cashflow(branch, today)
        odoo_b    = odoo_income.get(branch, {})
        odoo_total = odoo_b.get("total", 0.0)
        odoo_pays  = odoo_b.get("payments", [])

        branch_lines = [f"*{bname}*"]
        branch_issue = False

        # ── 1. Sheet 自查 ──────────────────────────────────────────────
        if today_bal is None:
            branch_lines.append("  _(no Cash Balance recorded today)_")
        elif prev_bal is None:
            branch_lines.append("  ⚠️ Cannot find previous closing balance")
            branch_issue = True
        elif "error" not in cf:
            income   = cf["income"]
            expenses = cf["expenses"]
            mvt_net  = cf["mvt_in"] - cf["mvt_out"]
            expected = prev_bal + income - expenses + mvt_net
            diff     = today_bal - expected
            if abs(diff) < 1.0:
                branch_lines.append(
                    f"  ✅ Sheet balanced  "
                    f"(₱{prev_bal:,.0f} +{income:,.0f} -{expenses:,.0f} ±{mvt_net:,.0f} = ₱{today_bal:,.0f})"
                )
            else:
                branch_issue = True
                branch_lines.append(
                    f"  ⚠️ Sheet arithmetic error — diff ₱{diff:+,.2f}\n"
                    f"     Expected ₱{expected:,.2f}  Actual ₱{today_bal:,.2f}\n"
                    f"     (prev={prev_bal:,.0f} +in={income:,.0f} -exp={expenses:,.0f} ±mvt={mvt_net:,.0f})"
                )

        # ── 2. Odoo vs Sheet 收入对比 ──────────────────────────────────
        sheet_income = cf.get("income", 0.0) if "error" not in cf else 0.0
        income_diff  = odoo_total - sheet_income

        if odoo_total == 0 and sheet_income == 0:
            branch_lines.append("  ✅ No cash income today (Odoo & Sheet both ₱0)")
        elif abs(income_diff) < 1.0:
            branch_lines.append(
                f"  ✅ Odoo income matches Sheet  (₱{odoo_total:,.2f})"
            )
        else:
            branch_issue = True
            if income_diff > 0:
                branch_lines.append(
                    f"  ⚠️ Odoo ₱{odoo_total:,.2f} > Sheet ₱{sheet_income:,.2f}  "
                    f"(diff +₱{income_diff:,.2f})\n"
                    f"     → Possible mis-entry: bank transfer recorded as Cash in Odoo?"
                )
            else:
                branch_lines.append(
                    f"  ⚠️ Sheet ₱{sheet_income:,.2f} > Odoo ₱{odoo_total:,.2f}  "
                    f"(diff ₱{income_diff:,.2f})\n"
                    f"     → Sheet income exceeds Odoo records — check Sheet entries"
                )

            # 列出 Odoo 各笔收款明细，方便定位问题
            if odoo_pays:
                branch_lines.append("     *Odoo Cash Receipts today:*")
                for pay in sorted(odoo_pays, key=lambda x: -x["amount"]):
                    branch_lines.append(
                        f"       • {pay['memo']}  ₱{pay['amount']:,.2f}"
                        + (f"  ({pay['order']})" if pay.get("order") else "")
                    )

        if branch_issue:
            any_issue = True

        lines.extend(branch_lines)
        lines.append("")

    if not any_issue:
        lines.append("_All stores OK_ ✅")

    _slack().chat_postMessage(
        channel=Config.SLACK_CHANNEL_MANAGEMENT,
        text="\n".join(lines),
    )
    logger.info("job_cash_balance_verify: posted for %s, any_issue=%s", today, any_issue)


# ── 每天 18:30：线上收款日报 ─────────────────────────────────────────────────

def job_online_payment_digest():
    """
    从 Odoo 拉今天所有已收款账单，同时统计 C0ARCDELSAJ 频道今天上传的凭证图片数量，
    合并发一条日报到管理频道。
    """
    from odoo.payments import get_new_payments

    today = date.today()
    since_utc = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    since_utc = since_utc - timedelta(hours=8)
    since_str = since_utc.strftime("%Y-%m-%d %H:%M:%S")

    try:
        payments = get_new_payments(since_str)
    except Exception as e:
        logger.error("job_online_payment_digest: Odoo error: %s", e)
        payments = []

    cash_keywords = {"cash", "cod", "cash on delivery"}
    online, cash_pay, other = [], [], []
    for p in payments:
        method = (p.get("payment_method") or "").lower()
        if any(k in method for k in cash_keywords):
            cash_pay.append(p)
        elif method and method != "—":
            online.append(p)
        else:
            other.append(p)

    proof_count = _count_payment_proofs(today)

    lines = [f"💳 *Online Payment Digest — {today.strftime('%Y-%m-%d')}*\n"]

    if online:
        total_online = sum(p["amount"] for p in online)
        lines.append(f"*Online Payments ({len(online)} | ₱{total_online:,.2f})*")
        for p in online:
            warehouse = f"[{p['warehouse']}] " if p.get("warehouse") and p["warehouse"] != "—" else ""
            lines.append(
                f"  • {warehouse}*{p['partner']}*  {p['currency']}{p['amount']:,.2f}"
                f"  _{p['payment_method']}_  {p['account']}"
            )
    else:
        lines.append("*Online Payments* — None recorded in Odoo today")

    if cash_pay:
        total_cash = sum(p["amount"] for p in cash_pay)
        lines.append(f"\n*Cash Payments ({len(cash_pay)} | ₱{total_cash:,.2f})*")
        for p in cash_pay:
            lines.append(f"  • *{p['partner']}*  {p['currency']}{p['amount']:,.2f}")

    if other:
        lines.append(f"\n*Other / Unclassified ({len(other)})*")
        for p in other:
            lines.append(f"  • *{p['partner']}*  {p['currency']}{p['amount']:,.2f}  _{p['payment_method']}_")

    if proof_count is not None:
        lines.append(
            f"\n📎 Payment proof images in <#{Config.SLACK_CHANNEL_PAYMENT_PROOFS}> today: *{proof_count}*"
        )

    _slack().chat_postMessage(
        channel=Config.SLACK_CHANNEL_MANAGEMENT,
        text="\n".join(lines),
    )
    logger.info("job_online_payment_digest: posted, %d online, %d cash, %d proofs",
                len(online), len(cash_pay), proof_count or 0)


def _count_payment_proofs(target_date: date) -> int | None:
    """统计 C0ARCDELSAJ 当天有多少条包含文件附件的消息。"""
    try:
        slack = _slack()
        tz_offset = 8 * 3600  # UTC+8
        day_start = datetime.combine(target_date, datetime.min.time())
        oldest = day_start.timestamp() - tz_offset
        latest = oldest + 86400

        result = slack.conversations_history(
            channel=Config.SLACK_CHANNEL_PAYMENT_PROOFS,
            oldest=str(oldest),
            latest=str(latest),
            limit=200,
        )
        msgs = result.get("messages", [])
        return sum(1 for m in msgs if m.get("files"))
    except Exception as e:
        logger.warning("_count_payment_proofs: %s", e)
        return None
