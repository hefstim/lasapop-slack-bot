# Odoo Slack Bot

Slack bot that bridges Odoo (sales/finance/inventory) with Slack for HEFS
Philippines. It polls Odoo via XML-RPC on a schedule, posts notifications and
interactive cards to Slack, lets employees update status with one click, and
exports data to Google Sheets for manual follow-up.

Slack identity: `@odoobot` (workspace: HEFS).

## How it runs

A single long-running Python process (`main.py`) does two things at once:

- **APScheduler** — runs all the background jobs in `scheduler/jobs.py` on
  fixed intervals/cron schedules (see table below).
- **Slack Bolt (Socket Mode)** — listens for `@mention` commands and button
  clicks in real time (`slack/handlers.py`).

It's managed by systemd as `odoo-slack-bot.service` (`Restart=always`), so it
comes back up automatically after a crash or reboot.

## Directory structure

```
main.py                  Entry point — builds the scheduler, starts Slack Socket Mode
config.py                All configuration, read from .env (see below)
db.py                     SQLite (bot_state.db) — polling cursors, reminder/dedup state
odoo/
  client.py               XML-RPC wrapper around Odoo. Auto-retries once on a
                           broken persistent connection (see "Known issues" below)
  customers.py             Dormant customers, new-customer contact-info check
  invoices.py               Overdue / legacy-overdue invoice queries
  orders.py                  New confirmed order polling
  payments.py                 New payment polling, payment method/account lookup
  quotations.py                Pending quotation queries
  inventory.py                  Low stock queries
  reports.py                     Daily/weekly/monthly sales report data
  integrity.py                    Data integrity checks (see below)
scheduler/jobs.py          One function per scheduled job — the only place that
                            calls Slack `chat.postMessage` for scheduled jobs
slack/
  app.py                  Slack Bolt App init + Socket Mode handler
  handlers.py               @mention commands + button click handlers
  messages.py                 All Slack Block Kit / text message builders
google_sheets/
  exporter.py              Dormant customer sheet (two tabs: main + tracker)
  invoice_exporter.py        Overdue / legacy invoice review sheets
  contact_exporter.py          Customer contact-info-missing export
```

> **Note on legacy duplicate files:** `handlers.py`, `messages.py`, `db.py`,
> `jobs.py`, `customers.py`, `reports.py` also exist at the **repo root**.
> These are stale leftovers from before the code was refactored into the
> `slack/`, `scheduler/`, `odoo/` packages. `main.py` only imports from the
> package versions — the root-level copies are dead code and safe to delete,
> just hasn't been done yet.

## Features

### `@mention` commands (in any channel the bot is in)

| Command | Action |
|---|---|
| `report` / `daily` | Today's sales report |
| `weekly` | This week's sales report |
| `monthly` | Last month's report |
| `monthly YYYY-MM` | Report for a specific month |
| `thismonth` | Current month progress report |
| `dormant` / `customers` | Export dormant customers to Google Sheet |
| `invoices` | Overdue invoice check |
| `invoicesheet` | Export ≤30-day overdue invoices to Google Sheet |
| `legacy` | Export >30-day overdue invoices to Google Sheet (manual review) |
| `quotations` / `quotes` | Pending quotation follow-up |
| `stock` | Low stock alert |
| `help` | Show command list |

### Scheduled jobs (`scheduler/jobs.py`, registered in `main.py`)

| Job | Schedule | Posts to |
|---|---|---|
| `job_new_orders` | every 5 min | sales |
| `job_new_payments` | every 5 min | finance |
| `job_new_customer_contact_check` | every 5 min | odoo-report |
| `job_integrity_check` | every 1 hour | odoo-report |
| `job_reactivation_check` | every 4 hours | management |
| `job_sync_wont_buy` | every 2 hours | — (no Slack post) |
| `job_low_stock` | daily 08:00 | warehouse |
| `job_dormant_customers` | daily 09:00 | sales |
| `job_overdue_invoices` | daily 09:30 | finance |
| `job_export_overdue_invoices` | daily 09:35 | finance |
| `job_pending_quotations` | daily 10:00 | sales |
| `job_daily_report` | daily 18:00 | management |
| `job_weekly_report` | Mon 08:30 | management |
| `job_export_legacy_invoices` | Mon 09:45 | finance |
| `job_monthly_report` | 1st of month 09:00 | management |

All "new X" pollers (`job_new_orders`, `job_new_payments`,
`job_new_customer_contact_check`) and `job_integrity_check` use a cursor
stored in `bot_state.db` (`notification_state` table) so they only ever
report on records created/changed **since the last successful run** — they
never re-scan or re-alert on old data. `job_integrity_check`'s
"order uninvoiced 30+ days" check additionally dedupes against an
`integrity_alerted_orders` table so the same stale order isn't reported every
hour.

### Interactive buttons

Clicking these updates the bot's local SQLite + strikes through the Slack
message — no need to open Odoo:

- Customers: `customer_contacted`, `customer_scheduled`, `customer_skipped`,
  `customer_excluded` (admin-only, see `ADMIN_USER_IDS`)
- Invoices: `invoice_followed`, `invoice_called`, `invoice_skipped`
- Quotations: `quote_followed`, `quote_closed`, `quote_skipped`
- Stock: `stock_handled`, `stock_ignored`
- `open_sheet` — plain URL button on Google Sheet notification cards, no
  server-side handler needed (Slack still sends an interactivity ping for
  these, which is why you'll see a harmless "no listener for open_sheet"
  message in the logs — not a bug).

### Data integrity check (`odoo/integrity.py`, hourly)

Flags, in `#odoo-report`:
- Posted customer invoices with amount ≤ 0
- Invoice due date earlier than invoice date
- `payment_state` inconsistent with the actual outstanding balance
- Same sale order with two posted invoices of the identical amount (possible
  duplicate billing)
- Confirmed sale orders that just crossed 30 days without being invoiced

### New order / payment notification cards

Both include: customer, order/invoice number, amount, sales rep, **warehouse**,
and for orders, **delivery status**. Payment cards also show payment method
and the receiving account (looked up via the reconciled `account.payment`
record).

## Configuration (`.env`)

See `.env.example` for the full list with comments. Key groups:

- `ODOO_URL` / `ODOO_DB` / `ODOO_USERNAME` / `ODOO_PASSWORD` — Odoo XML-RPC login
- `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_SIGNING_SECRET` — from the Slack app config
- `SLACK_CHANNEL_SALES` / `_FINANCE` / `_WAREHOUSE` / `_MANAGEMENT` — channel **names** (bot must be a member)
- `SLACK_CHANNEL_ODOO_REPORT` — **channel ID**, not name (it's private; the
  bot's token lacks `groups:read` so it can't resolve a private channel name
  to an ID itself — get the ID from the channel's "Copy link")
- `DORMANT_CUSTOMER_DAYS`, `OVERDUE_INVOICE_DAYS`, `OVERDUE_INVOICE_MAX_DAYS`,
  `QUOTATION_FOLLOWUP_DAYS`, `LOW_STOCK_THRESHOLD` — business-rule thresholds
- `MONTHLY_SALES_TARGET`, `CONTACTED_COOLDOWN_DAYS`
- `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_SHEET_ID` — Google Sheets exports
- `ADMIN_USER_IDS` — comma-separated Slack user IDs allowed to permanently exclude customers
- `SALESPERSON_MAP` — `Odoo salesperson name=Slack user ID` pairs, comma-separated, for `@mention` pings

## Known issues / things to know

- **Stale XML-RPC connections**: `OdooClient` keeps one persistent
  `xmlrpc.client.ServerProxy` per process. If the socket breaks (timeout, the
  server closing it mid-request), every later call used to fail forever with
  `Request-sent`/`Idle` errors until the process restarted. `execute()` now
  auto-recreates the connection and retries once on `http.client.HTTPException`/`OSError`.
- **`#odoo-report` is a private channel** — the bot can't list private
  channels via the API (missing `groups:read` scope). Always configure it by
  channel ID, not name.
- **Root-level duplicate files** — see the note in "Directory structure" above.
- The dormant-customer Google Sheet workflow (`google_sheets/exporter.py`) is
  two-way: employees write `Status`/`Notes` in the sheet, the bot reads it
  back on the next export and reacts (moves to tracker sheet, syncs "Won't
  Buy" to the local exclude-list).

## Local state (`bot_state.db`, SQLite, git-ignored)

| Table | Purpose |
|---|---|
| `notification_state` | Generic key/value cursor store (`last_order_id`, `last_payment_ts`, `integrity_last_check_ts`, `last_customer_check_ts`, ...) |
| `customer_reminders`, `invoice_reminders`, `quotation_reminders`, `stock_alerts` | One row per reminder card sent — backs the interactive buttons and same-day dedup |
| `excluded_customers` | "Won't Buy" blacklist |
| `integrity_alerted_orders` | Order IDs already alerted on by the 30-day-uninvoiced check |

**This file is not in git.** Losing it doesn't lose Odoo/Slack data, but it
does reset all polling cursors — after restoring from a fresh/empty
`bot_state.db`, the first run of each "new X" poller will treat everything
since *right now* as new (cursors default to "now" on first read, so it
won't dump years of history — but it will lose the "already reminded today"
dedup state, so a same-day reminder could in theory be sent twice).

## Migrating to a new server / handing off

1. **Clone the repo**
   ```bash
   git clone git@github.com:hefstim/odoo-slack-bot.git
   cd odoo-slack-bot
   ```
2. **Recreate the virtualenv** (don't copy `venv/` — it's tied to the old
   machine's Python build)
   ```bash
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```
3. **Copy the secrets/state that are deliberately git-ignored**, from the old
   server to the new one (e.g. via `scp`):
   - `.env` (copy `.env.example` and fill in real values if starting fresh)
   - `google_credentials.json` (Google service account key)
   - `bot_state.db` (optional — carry it over to preserve polling cursors and
     avoid duplicate same-day reminders; safe to skip if starting clean)
4. **Install the systemd unit** (copy `/etc/systemd/system/odoo-slack-bot.service`
   from the old server, update `WorkingDirectory`/`ExecStart` paths if they
   changed), then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now odoo-slack-bot.service
   ```
5. **Verify**: `journalctl -u odoo-slack-bot.service -f` — look for
   `Odoo connection OK (uid=...)` and `Scheduler started` with no tracebacks.
6. **Re-point Slack** only if the bot's tokens changed — same `SLACK_BOT_TOKEN`/
   `SLACK_APP_TOKEN` work from any host, no Slack-side reconfiguration needed
   for a same-app migration.

### Handing this off to a different project/team

The Odoo-specific knowledge lives entirely in `odoo/*.py` (one file per
domain: customers, invoices, orders, payments, quotations, inventory,
reports, integrity) and `config.py`. The Slack-specific knowledge lives in
`slack/*.py` and `slack/messages.py`. If reusing just the "Odoo XML-RPC + retry"
or "Google Sheets export with editable columns + protection" parts for another
bot, `odoo/client.py` and `google_sheets/exporter.py`'s helper functions
(`_get_or_create_ws`, `_protect_cols`, formatting helpers) are the reusable
building blocks — everything else is business logic specific to this Odoo
instance's data shape.
