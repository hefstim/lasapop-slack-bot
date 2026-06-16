"""Daily and weekly sales report data."""
from datetime import datetime, timedelta, date
from config import Config
from odoo.client import get_client


def _build_by_person(orders: list) -> list[tuple]:
    """Return sorted list of (name, {amount, orders}) tuples."""
    by_person: dict[str, dict] = {}
    for o in orders:
        name = o["user_id"][1] if isinstance(o.get("user_id"), (list, tuple)) else "Unassigned"
        if name not in by_person:
            by_person[name] = {"amount": 0, "orders": 0}
        by_person[name]["amount"] += o["amount_total"]
        by_person[name]["orders"] += 1
    return sorted(by_person.items(), key=lambda x: x[1]["amount"], reverse=True)


def get_daily_report(target_date: date | None = None) -> dict:
    client = get_client()
    d = target_date or date.today()
    date_start = d.strftime("%Y-%m-%d 00:00:00")
    date_end   = d.strftime("%Y-%m-%d 23:59:59")

    orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]),
         ("date_order", ">=", date_start), ("date_order", "<=", date_end)],
        ["name", "partner_id", "amount_total", "user_id"],
    )
    invoices_paid = client.search_read(
        "account.move",
        [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
         ("payment_state", "=", "paid"), ("invoice_date", "=", d.isoformat())],
        ["amount_total"],
    )
    new_customers = client.search_read(
        "res.partner",
        [("customer_rank", ">", 0),
         ("create_date", ">=", date_start), ("create_date", "<=", date_end)],
        ["name"],
    )

    # Monthly target progress
    month_start = d.strftime("%Y-%m-01 00:00:00")
    month_orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]),
         ("date_order", ">=", month_start), ("date_order", "<=", date_end)],
        ["amount_total"],
    )
    month_sales = sum(o["amount_total"] for o in month_orders)

    days_in_month = (date(d.year, d.month % 12 + 1, 1) - timedelta(days=1)).day if d.month < 12 \
        else 31
    days_left = days_in_month - d.day

    return {
        "date":           d.strftime("%B %d, %Y"),
        "order_count":    len(orders),
        "total_sales":    sum(o["amount_total"] for o in orders),
        "total_collected": sum(i["amount_total"] for i in invoices_paid),
        "new_customers":  len(new_customers),
        "by_person":      _build_by_person(orders),
        "month_sales":    month_sales,
        "days_left":      days_left,
    }


def get_monthly_report(year: int | None = None, month: int | None = None) -> dict:
    client = get_client()
    today = date.today()
    y = year or today.year
    m = month or today.month

    # Last day of the target month
    if m == 12:
        last_day = date(y, 12, 31)
    else:
        last_day = date(y, m + 1, 1) - timedelta(days=1)

    date_start = f"{y}-{m:02d}-01 00:00:00"
    date_end   = last_day.strftime("%Y-%m-%d 23:59:59")
    month_label = date(y, m, 1).strftime("%B %Y")

    orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]),
         ("date_order", ">=", date_start), ("date_order", "<=", date_end)],
        ["name", "partner_id", "amount_total", "user_id"],
    )
    invoices_paid = client.search_read(
        "account.move",
        [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
         ("payment_state", "=", "paid"),
         ("invoice_date", ">=", f"{y}-{m:02d}-01"),
         ("invoice_date", "<=", last_day.isoformat())],
        ["amount_total"],
    )
    new_customers = client.search_read(
        "res.partner",
        [("customer_rank", ">", 0),
         ("create_date", ">=", date_start), ("create_date", "<=", date_end)],
        ["name"],
    )
    new_invoices = client.search_read(
        "account.move",
        [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
         ("invoice_date", ">=", f"{y}-{m:02d}-01"),
         ("invoice_date", "<=", last_day.isoformat())],
        ["amount_total"],
    )

    # Top customers
    top_raw: dict[int, dict] = {}
    for o in orders:
        if isinstance(o.get("partner_id"), (list, tuple)):
            pid, pname = o["partner_id"][0], o["partner_id"][1]
            if pid not in top_raw:
                top_raw[pid] = {"name": pname, "amount": 0, "orders": 0}
            top_raw[pid]["amount"] += o["amount_total"]
            top_raw[pid]["orders"] += 1
    top_customers = sorted(top_raw.values(), key=lambda x: x["amount"], reverse=True)[:10]

    total_sales     = sum(o["amount_total"] for o in orders)
    total_collected = sum(i["amount_total"] for i in invoices_paid)
    total_invoiced  = sum(i["amount_total"] for i in new_invoices)

    return {
        "month_label":     month_label,
        "order_count":     len(orders),
        "total_sales":     total_sales,
        "total_collected": total_collected,
        "total_invoiced":  total_invoiced,
        "new_customers":   len(new_customers),
        "by_person":       _build_by_person(orders),
        "top_customers":   top_customers,
    }


def get_weekly_report(week_start: date | None = None) -> dict:
    client = get_client()
    today = date.today()
    if week_start is None:
        week_start = today - timedelta(days=today.weekday() + 7)
    week_end = week_start + timedelta(days=6)

    date_start = week_start.strftime("%Y-%m-%d 00:00:00")
    date_end   = week_end.strftime("%Y-%m-%d 23:59:59")

    orders = client.search_read(
        "sale.order",
        [("state", "in", ["sale", "done"]),
         ("date_order", ">=", date_start), ("date_order", "<=", date_end)],
        ["name", "partner_id", "amount_total", "user_id", "date_order"],
    )
    invoices_paid = client.search_read(
        "account.move",
        [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
         ("payment_state", "=", "paid"),
         ("invoice_date", ">=", week_start.isoformat()),
         ("invoice_date", "<=", week_end.isoformat())],
        ["amount_total"],
    )
    new_customers = client.search_read(
        "res.partner",
        [("customer_rank", ">", 0),
         ("create_date", ">=", date_start), ("create_date", "<=", date_end)],
        ["name"],
    )

    top_customers_raw: dict[int, dict] = {}
    for o in orders:
        if isinstance(o.get("partner_id"), (list, tuple)):
            pid, pname = o["partner_id"][0], o["partner_id"][1]
            if pid not in top_customers_raw:
                top_customers_raw[pid] = {"name": pname, "amount": 0, "orders": 0}
            top_customers_raw[pid]["amount"]  += o["amount_total"]
            top_customers_raw[pid]["orders"]  += 1
    top_customers = sorted(top_customers_raw.values(), key=lambda x: x["amount"], reverse=True)[:5]

    return {
        "week_label":      f"{week_start.strftime('%b %d')} — {week_end.strftime('%b %d, %Y')}",
        "order_count":     len(orders),
        "total_sales":     sum(o["amount_total"] for o in orders),
        "total_collected": sum(i["amount_total"] for i in invoices_paid),
        "new_customers":   len(new_customers),
        "by_person":       _build_by_person(orders),
        "top_customers":   top_customers,
    }
