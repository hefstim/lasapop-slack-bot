import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Odoo
    ODOO_URL = os.getenv("ODOO_URL", "")
    ODOO_DB = os.getenv("ODOO_DB", "")
    ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")

    # Slack
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
    SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

    # Slack Channels
    SLACK_CHANNEL_SALES = os.getenv("SLACK_CHANNEL_SALES", "sales")
    SLACK_CHANNEL_FINANCE = os.getenv("SLACK_CHANNEL_FINANCE", "finance")
    SLACK_CHANNEL_WAREHOUSE = os.getenv("SLACK_CHANNEL_WAREHOUSE", "warehouse")
    SLACK_CHANNEL_MANAGEMENT = os.getenv("SLACK_CHANNEL_MANAGEMENT", "general")
    SLACK_CHANNEL_ODOO_REPORT = os.getenv("SLACK_CHANNEL_ODOO_REPORT", "C0BAQCGCY8L")
    # 员工每日现金支出上报频道
    SLACK_CHANNEL_CASH = os.getenv("SLACK_CHANNEL_CASH", "")
    # 线上支付凭证上传频道（只读，统计图片数量）
    SLACK_CHANNEL_PAYMENT_PROOFS = os.getenv("SLACK_CHANNEL_PAYMENT_PROOFS", "C0ARCDELSAJ")
    # 报销审批卡片发布频道
    SLACK_CHANNEL_EXPENSE_APPROVAL = os.getenv("SLACK_CHANNEL_EXPENSE_APPROVAL", "")

    # Business Rules
    DORMANT_CUSTOMER_DAYS = int(os.getenv("DORMANT_CUSTOMER_DAYS", "90"))
    OVERDUE_INVOICE_DAYS = int(os.getenv("OVERDUE_INVOICE_DAYS", "1"))
    OVERDUE_INVOICE_MAX_DAYS = int(os.getenv("OVERDUE_INVOICE_MAX_DAYS", "30"))
    QUOTATION_FOLLOWUP_DAYS = int(os.getenv("QUOTATION_FOLLOWUP_DAYS", "7"))
    LOW_STOCK_THRESHOLD = float(os.getenv("LOW_STOCK_THRESHOLD", "10"))

    # Timezone
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")

    # Monthly sales target (0 = not set)
    MONTHLY_SALES_TARGET = float(os.getenv("MONTHLY_SALES_TARGET", "0"))
    CONTACTED_COOLDOWN_DAYS = int(os.getenv("CONTACTED_COOLDOWN_DAYS", "14"))

    # Google Sheets
    GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

    # Admin user IDs allowed to permanently exclude customers
    ADMIN_USER_IDS: set[str] = set(
        uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
    )

    # Salesperson name -> Slack user ID mapping
    _RAW_MAP = os.getenv("SALESPERSON_MAP", "")
    SALESPERSON_MAP: dict[str, str] = {}
    if _RAW_MAP:
        for pair in _RAW_MAP.split(","):
            pair = pair.strip()
            if "=" in pair:
                name, uid = pair.split("=", 1)
                SALESPERSON_MAP[name.strip()] = uid.strip()

    # Slack user ID -> Odoo hr.employee id, for the `expense` mention command
    _RAW_EXPENSE_MAP = os.getenv("EXPENSE_EMPLOYEE_MAP", "")
    EXPENSE_EMPLOYEE_MAP: dict[str, int] = {}
    if _RAW_EXPENSE_MAP:
        for pair in _RAW_EXPENSE_MAP.split(","):
            pair = pair.strip()
            if "=" in pair:
                slack_uid, employee_id = pair.split("=", 1)
                EXPENSE_EMPLOYEE_MAP[slack_uid.strip()] = int(employee_id.strip())
