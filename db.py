"""SQLite state tracker — records what reminders were sent and what actions were taken."""
import logging
import os
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

from config import Config

_BASE = Path(__file__).parent
DB_PATH = str(_BASE / "bot_state.db")

_SQLITE_MAGIC = b"SQLite format 3\x00"
logger = logging.getLogger(__name__)


def _check_db_file():
    """If DB file exists but is not a valid SQLite file, back it up and remove it."""
    path = Path(DB_PATH)
    if not path.exists():
        return
    with path.open("rb") as f:
        header = f.read(16)
    if header != _SQLITE_MAGIC:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"bot_state.db.corrupted_{ts}")
        shutil.move(str(path), str(backup))
        logger.error(
            "DB file was not a valid SQLite database — backed up to %s and will recreate.",
            backup.name,
        )


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    _check_db_file()
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customer_reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id    INTEGER NOT NULL,
                partner_name  TEXT    NOT NULL,
                salesperson   TEXT,
                days_inactive INTEGER,
                last_order_date TEXT,
                last_order_amount REAL DEFAULT 0,
                remind_date   TEXT    NOT NULL,
                slack_ts      TEXT,
                slack_channel TEXT,
                status        TEXT    DEFAULT 'pending',
                note          TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS invoice_reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id    INTEGER NOT NULL,
                invoice_name  TEXT    NOT NULL,
                partner_name  TEXT    NOT NULL,
                amount_residual REAL  DEFAULT 0,
                due_date      TEXT,
                days_overdue  INTEGER DEFAULT 0,
                remind_date   TEXT    NOT NULL,
                slack_ts      TEXT,
                slack_channel TEXT,
                status        TEXT    DEFAULT 'pending',
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quotation_reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id      INTEGER NOT NULL,
                order_name    TEXT    NOT NULL,
                partner_name  TEXT    NOT NULL,
                amount_total  REAL    DEFAULT 0,
                create_date   TEXT,
                days_pending  INTEGER DEFAULT 0,
                salesperson   TEXT,
                remind_date   TEXT    NOT NULL,
                slack_ts      TEXT,
                slack_channel TEXT,
                status        TEXT    DEFAULT 'pending',
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notification_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS excluded_customers (
                partner_id    INTEGER PRIMARY KEY,
                partner_name  TEXT    NOT NULL,
                excluded_by   TEXT,
                excluded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stock_alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id    INTEGER NOT NULL,
                product_name  TEXT    NOT NULL,
                qty_on_hand   REAL    DEFAULT 0,
                reorder_point REAL    DEFAULT 0,
                alert_date    TEXT    NOT NULL,
                slack_ts      TEXT,
                slack_channel TEXT,
                status        TEXT    DEFAULT 'pending',
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS integrity_alerted_orders (
                order_id   INTEGER PRIMARY KEY,
                alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    os.chmod(DB_PATH, 0o600)


# ── helpers ──────────────────────────────────────────────────────────────────

def already_reminded_today(table: str, id_col: str, record_id: int) -> bool:
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE {id_col}=? AND remind_date=? AND status='pending'",
            (record_id, today)
        ).fetchone()
    return row is not None


def save_customer_reminder(partner_id, partner_name, salesperson, days_inactive,
                           last_order_date, last_order_amount, slack_ts, channel) -> int:
    today = date.today().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO customer_reminders
               (partner_id,partner_name,salesperson,days_inactive,last_order_date,
                last_order_amount,remind_date,slack_ts,slack_channel)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (partner_id, partner_name, salesperson, days_inactive,
             last_order_date, last_order_amount, today, slack_ts, channel)
        )
        return cur.lastrowid


def save_invoice_reminder(invoice_id, invoice_name, partner_name, amount_residual,
                          due_date, days_overdue, slack_ts, channel) -> int:
    today = date.today().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO invoice_reminders
               (invoice_id,invoice_name,partner_name,amount_residual,due_date,
                days_overdue,remind_date,slack_ts,slack_channel)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (invoice_id, invoice_name, partner_name, amount_residual,
             due_date, days_overdue, today, slack_ts, channel)
        )
        return cur.lastrowid


def save_quotation_reminder(order_id, order_name, partner_name, amount_total,
                            create_date, days_pending, salesperson, slack_ts, channel) -> int:
    today = date.today().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO quotation_reminders
               (order_id,order_name,partner_name,amount_total,create_date,
                days_pending,salesperson,remind_date,slack_ts,slack_channel)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (order_id, order_name, partner_name, amount_total,
             create_date, days_pending, salesperson, today, slack_ts, channel)
        )
        return cur.lastrowid


def save_stock_alert(product_id, product_name, qty_on_hand, reorder_point, slack_ts, channel) -> int:
    today = date.today().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO stock_alerts
               (product_id,product_name,qty_on_hand,reorder_point,alert_date,slack_ts,slack_channel)
               VALUES (?,?,?,?,?,?,?)""",
            (product_id, product_name, qty_on_hand, reorder_point, today, slack_ts, channel)
        )
        return cur.lastrowid


def update_status(table: str, row_id: int, status: str, note: str = ""):
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {table} SET status=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, note, row_id)
        )


# ── Notification state (polling cursors) ─────────────────────────────────────

def get_state(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM notification_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notification_state (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)",
            (key, value)
        )


# ── Excluded customers ────────────────────────────────────────────────────────

def exclude_customer(partner_id: int, partner_name: str, excluded_by: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO excluded_customers (partner_id, partner_name, excluded_by) VALUES (?,?,?)",
            (partner_id, partner_name, excluded_by)
        )


def get_all_excluded_customers() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM excluded_customers").fetchall()


def remove_excluded_customer(partner_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM excluded_customers WHERE partner_id=?", (partner_id,))


def get_excluded_partner_ids() -> set[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT partner_id FROM excluded_customers").fetchall()
    return {r["partner_id"] for r in rows}


def get_row(table: str, row_id: int):
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()


# ── Integrity check alert dedup ───────────────────────────────────────────────

def get_alerted_order_ids() -> set[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT order_id FROM integrity_alerted_orders").fetchall()
    return {r["order_id"] for r in rows}


def mark_orders_alerted(order_ids: list[int]):
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO integrity_alerted_orders (order_id) VALUES (?)",
            [(oid,) for oid in order_ids]
        )
