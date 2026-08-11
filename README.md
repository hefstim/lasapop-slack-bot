# Odoo ↔ Slack Bots

One codebase, two independent live deployments — HEFS (Philippines) and
LasaPop. Both bridge Odoo (XML-RPC) with Slack: poll Odoo on a schedule, post
notification/interactive cards, let staff act with one click, export to
Google Sheets. Each deployment is its own systemd service, own `.env`, own
`bot_state.db`, and has drifted from the other in features — **but both
directories are git checkouts of the exact same GitHub repo**, see
"⚠️ Shared git remote" below before doing any git operation.

| | HEFS | LasaPop |
|---|---|---|
| Path | `/opt/odoo-slack-bot` (`japan_oracle`, 150.230.63.12) | `/opt/lasapop-slack-bot` (same server) |
| systemd service | `odoo-slack-bot.service` | `lasapop-slack-bot.service` |
| Slack identity | `@odoobot`, workspace HEFS | `@lasapop_bot`, workspace LasaPop |
| Odoo | `https://www.02ph.com`, db from `.env` | `http://localhost:8069`, db `lasapop` |
| Operational status | Fully live — real order/payment/invoice volume | **Business not yet operational** — most Odoo-sync jobs are disabled in `main.py` (see below); only the Slack-native expense/cash features are live |

## Shared architecture

A single long-running Python process (`main.py`) does two things at once:

- **APScheduler** — background jobs in `scheduler/jobs.py`, cron/interval scheduled.
- **Slack Bolt (Socket Mode)** — `@mention` commands + button clicks, in `slack/handlers.py`.

```
main.py                  Entry point — builds the scheduler, starts Slack Socket Mode
config.py                All configuration, read from .env
db.py                    SQLite (bot_state.db) — polling cursors, reminder/dedup state,
                          DB-corruption self-heal (see "Known issues" below)
odoo/
  client.py              XML-RPC wrapper. Auto-retries once on a broken persistent connection.
  orders.py / payments.py / invoices.py / quotations.py / inventory.py
  customers.py / reports.py / integrity.py
  expenses.py             LasaPop only — hr.expense submit/approve/refuse via Odoo
scheduler/
  jobs.py                 One function per scheduled job — the only place scheduled
                           jobs call Slack chat.postMessage
  cash_jobs.py             LasaPop only — cash-balance verification (built, currently
                           NOT wired into main.py's scheduler, see below)
slack/
  app.py                  Slack Bolt App init + Socket Mode handler
  handlers.py              @mention commands + button/shortcut/modal handlers
  messages.py               Block Kit / text message builders
google_sheets/
  exporter.py / invoice_exporter.py / contact_exporter.py / cash_reader.py (LasaPop)
```

> **Dead code warning:** `handlers.py`, `messages.py`, `db.py`, `jobs.py`,
> `customers.py`, `reports.py` also exist at the **repo root** in both
> deployments — pre-refactor leftovers, not imported by `main.py`. If you're
> ever unsure which file is live, check `main.py`'s imports, not the root.

### `bot_state.db` (SQLite, git-ignored, per-deployment)

| Table | Purpose |
|---|---|
| `notification_state` | key/value polling cursors: `last_order_id`, `last_payment_ts`, `integrity_last_check_ts`, `last_customer_check_ts` |
| `customer_reminders`, `invoice_reminders`, `quotation_reminders`, `stock_alerts` | one row per reminder card — backs interactive buttons + same-day dedup |
| `excluded_customers` | "Won't Buy" blacklist |
| `integrity_alerted_orders` | orders already alerted on by the 30-day-uninvoiced check |
| `expense_requests`, `cash_topups` | LasaPop only — expense approval + cash fund tracking |

Hourly (HEFS) / every-15-min (LasaPop) rolling backups live in `backups/`.

## HEFS — live jobs (`scheduler/jobs.py`, all currently active)

| Job | Schedule | Posts to |
|---|---|---|
| `job_new_orders`, `job_new_payments`, `job_new_customer_contact_check` | every 5 min, 08–19h | sales / finance / odoo-report |
| `job_integrity_check` | hourly | odoo-report |
| `job_reactivation_check` | every 4h | management |
| `job_sync_wont_buy` | every 2h | — |
| `job_low_stock` | daily 08:00 | warehouse |
| `job_dormant_customers` | daily 09:00 | sales |
| `job_overdue_invoices` / `job_export_overdue_invoices` | daily 09:30 / 09:35 | finance |
| `job_pending_quotations` | daily 10:00 | sales |
| `job_daily_report` | daily 18:00 | management |
| `job_weekly_report` | Mon 08:30 | management |
| `job_export_legacy_invoices` | Mon 09:45 | finance |
| `job_monthly_report` | 1st of month 09:00 | management |

**Channel gotcha:** in HEFS's `.env`, `SLACK_CHANNEL_SALES` = `SLACK_CHANNEL_FINANCE`
= `SLACK_CHANNEL_WAREHOUSE` = `SLACK_CHANNEL_MANAGEMENT` = literal name `odoo-report`
— i.e. **every job posts to the same one channel**, "odoo-report" (channel ID
`C0BAQCGCY8L`, resolved by name at runtime). A *separate* config var,
`SLACK_CHANNEL_ODOO_REPORT`, is set to a **different** channel ID
(`C0ARTKSPG81`) and is used by only `job_integrity_check` and
`job_new_customer_contact_check`. Don't assume "odoo-report" refers to one
channel — check which config var a given job actually reads.

`@mention` commands: `report`/`daily`, `weekly`, `monthly [YYYY-MM]`,
`thismonth`, `dormant`/`customers`, `invoices`, `invoicesheet`, `legacy`,
`quotations`/`quotes`, `stock`, `help`.

## LasaPop — live vs disabled

**Only `job_integrity_check` (hourly) and `db.backup_db` (every 15 min) are
active.** Every other job from the shared `scheduler/jobs.py` list above —
new orders/payments, reports, dormant customers, overdue invoices, low
stock, reactivation, Won't-Buy sync — is commented out in `main.py` with
`# [停用-未运营]` ("disabled — not operational"). This is intentional: the
LasaPop business isn't running real order volume through Odoo yet (confirmed
2026-08-11: `sale.order` count is 0). **If `job_new_orders` "isn't working"
on LasaPop, check `main.py` first — it's disabled by design, not broken.**

There's also a whole cash-verification subsystem (`scheduler/cash_jobs.py`,
`google_sheets/cash_reader.py` — `job_cash_balance_verify`, nightly Odoo-vs-Sheet
cash reconciliation) that exists in code but currently has **no `add_job()`
call wiring it into the scheduler at all** (not even commented out — just
never added). Built, not live.

### What *is* live: expense approval + cash top-ups (Slack-native, no Odoo order dependency)

`@mention` commands: `expense <amount> <description>`, `expensetotal`,
`cashin <amount> <note>` (admin only, `Config.ADMIN_USER_IDS`), `balance`,
`help`. Same actions are also available as Slack shortcuts (⚡ icon): *提交支出*,
*查询现金统计*, *充值现金*.

Flow: `expense` mention → creates + submits a draft `hr.expense`
(`odoo/expenses.py::create_and_submit_expense`) → posts an approval card to
`SLACK_CHANNEL_EXPENSE_APPROVAL` (`#exp-approval`) → admin clicks
Approve/Refuse → `approve_expense`/`refuse_expense` (refuse goes through
`hr.expense.refuse.wizard`; `action_refuse()` on the record itself only opens
a UI dialog, doesn't refuse) → local `expense_requests` row updated, Slack
message edited in place. Optional receipt photos attached alongside the
mention get downloaded and saved as an `ir.attachment` via `attach_receipt` —
failure there logs but never blocks the expense submission.

`Config.EXPENSE_EMPLOYEE_MAP` (`.env`, `SlackUserID=OdooEmployeeID` pairs) is
the only source of Slack→Odoo employee identity; a submitter not in the map
gets rejected with a friendly message, not a crash.

This feature deliberately never posts to formal Odoo accounting (no cash
journal, no `action_post`/`action_pay`). Real bookkeeping is a separate
monthly lump-sum cash withdrawal booked via a different AI-driven MCP tool
(`record_expense` in `/root/.hermes/odoo_mcp_lasapop.py`, a completely
different Odoo model). This feature's `hr.expense` records sitting at
`approved` **are** the itemized detail ledger for where that lump sum went —
don't suggest "closing the loop" into formal accounting unless the actual
goal changes; it would duplicate/conflict with the existing lump booking.

## ⚠️ Shared git remote — read before any git operation

Both `/opt/odoo-slack-bot` and `/opt/lasapop-slack-bot` are `git clone`s of
**the same repo**, `git@github.com:hefstim/odoo-slack-bot.git`, both on
`main`. LasaPop was bootstrapped from HEFS's code and diverged (expense/cash
features, disabled jobs, different config) largely through **uncommitted
local edits that were never pushed** — so `git status` in either directory
routinely shows a pile of pre-existing modified/untracked files that predate
whatever you're currently working on. Before committing or pushing:

1. **Check `git status` first and don't blindly `git add -A`** — a file may
   already carry unrelated historical changes mixed with the ones you just
   made. If so, isolate your hunk with `git diff > /tmp/x.patch`, hand-edit
   the patch down to just your change, and `git apply --cached` it — don't
   commit the whole file diff.
2. **Never commit `.env.bak-*` files** (LasaPop has several sitting
   untracked — likely contain secrets).
3. **Pushing from one side moves `origin/main` out from under the other.**
   After pushing from HEFS, LasaPop's local `main` needs
   `git fetch && git rebase --autostash origin/main` before it can push (and
   vice versa). `--autostash` matters — both dirs have uncommitted local-only
   work that must survive the rebase.
4. **If the same logical fix is ported to both sides, expect the rebase to
   either auto-drop your commit** (git recognizes the patch is already
   upstream — no push needed, you're already up to date) **or conflict**
   (if the two versions differ even slightly, e.g. one has extra
   thread-safety or feature code the other doesn't). Resolve by hand — for
   an empty-old-side hunk in particular, don't trust a scripted/regex
   conflict resolution; a lazy regex can silently swallow real code sitting
   between two conflict markers. Pull the conflicted file down, resolve
   hunk-by-hunk with visual confirmation, verify `python3 -m py_compile`
   and no duplicate `def` before continuing the rebase.

## Known issues / debugging playbook

**"`job_new_orders` keeps getting skipped" (`journalctl` shows
`Execution of job "job_new_orders..." skipped: maximum number of running
instances reached (1)`)** — the job is stuck, not idle. Check
`journalctl -u <service> --since <job's last "Running job" line>` for what
it's doing. Most likely cause below.

**DB-corruption cursor reset → full-history replay flood (root-caused
2026-08-11, HEFS):** `db.py`'s `_check_db_file()` self-heals a corrupted
`bot_state.db` (bad SQLite header) by backing it up
(`bot_state.db.corrupted_<timestamp>`) and recreating an empty schema —
called before every DB connection, so it can trigger mid-run, not just at
startup. Every cursor except `last_order_id` defaults safely to "now" at its
own `get_state(key, now)` call site, so a reset there just means "don't
double-report today's stuff." **`last_order_id` defaults to `"0"`** — so an
unset cursor makes `job_new_orders` fetch *every confirmed order ever
placed* and post them to Slack one at a time, 1.2s apart. On HEFS this
posted ~808 historical orders into the shared "odoo-report" channel over 19
minutes before a manual `systemctl restart` stopped it (restart works
because `main()`'s own startup bootstrap re-initializes `last_order_id` to
the current max — the bug was that `_check_db_file()`'s in-place recovery
didn't do the same thing). **Fixed** in both deployments: `_check_db_file()`
now re-initializes `last_order_id` itself, using the connection it already
has open — not via `set_state()`, which goes through `get_conn()` →
`_check_db_file()` → the same non-reentrant `_check_lock`, which would
deadlock the entire process. If you ever touch `_check_db_file()` again,
keep any cursor-repair logic on the already-open `conn`, never call
`set_state()`/`get_conn()` from inside it.

**`app_mention` fires unreliably** — long stretches of zero deliveries, but
when it *does* fire it can also be redelivered twice for one message
(observed directly: two `hr.expense` records from one `expense` mention).
Fixed in both deployments: `handle_mention`'s body moved into a shared
`_dispatch_mention()`, wired to both `app_mention` and a `message`-event
fallback (checks `<@bot_user_id>` in text), with a `(channel, ts)` dedup
guard so it doesn't matter which event(s) fire or how many times.

**LasaPop only — `<@BOTID>` mentions silently drop:** copy-pasting a
previous message instead of retyping `@` + autocomplete can carry a
`<@BOTID>` reference (the app's `bot_id`) instead of `<@USERID>`
(`bot_user_id`). The plain message-event fallback above only checked
`bot_user_id`, so these matched neither `app_mention` nor the fallback and
vanished with zero log trace. Fixed: `_get_bot_id()` caches `auth_test()`'s
`bot_id`, and the fallback now accepts either form.

**Generic `update_status(table, row_id, status, note="")` requires a `note`
column** — it unconditionally writes `SET status=?, note=?, ...`. Any new
SQLite table routed through it (see the four reminder/alert tables plus
`expense_requests`) must have a `note TEXT` column, or every button click on
that table silently fails at the `chat_update`-never-reached point (fixed
2026-08-04 after `invoice_reminders`/`quotation_reminders`/`stock_alerts`
were missing it for a long time — same bug immediately caught on
`expense_requests` since it was still being tested).

**LasaPop's Odoo XML-RPC raises spurious `cannot marshal None` on action
methods** (`action_submit`, `action_approve`, etc.) even though the
underlying write succeeds — `odoo/expenses.py::_safe_execute()` swallows
that specific fault text. Expect the same on any new action-method call
against this instance.

**`SLACK_CHANNEL_ODOO_REPORT` is a private channel** — configure it by
channel **ID**, never name; the bot's token lacks `groups:read` and can't
resolve a private channel name itself.

**Deleting a Slack message does not un-send an already-delivered mobile
push notification.** If a flood already reached phones before you clean up
the channel, the channel history can be scrubbed via `chat.delete`
(requires `channels:history`/`groups:history` bot scope, which the default
token doesn't have — add it in the Slack app's OAuth & Permissions page and
reinstall to the workspace; reinstalling doesn't always rotate the token,
diff against `.env` to check), but already-pushed phone notifications are
gone from Slack's side and can only be cleared client-side.

## Diagnostic quick-reference

```bash
# service health
sudo systemctl status odoo-slack-bot.service        # or lasapop-slack-bot.service
sudo journalctl -u odoo-slack-bot.service -f

# is a job stuck? (compare "Running job" vs next "executed successfully")
sudo journalctl -u odoo-slack-bot.service --no-pager | grep -i job_new_orders | tail -20

# polling cursors
sqlite3 /opt/odoo-slack-bot/bot_state.db "select * from notification_state;"

# has the DB self-heal ever fired?
ls -la /opt/odoo-slack-bot/bot_state.db.corrupted_*
```

## Migrating / handing off

1. `git clone git@github.com:hefstim/odoo-slack-bot.git` (see the shared-remote
   warning above — know which deployment's local drift you're bringing along,
   since a fresh clone gets neither HEFS's nor LasaPop's uncommitted extras).
2. `python3 -m venv venv && venv/bin/pip install -r requirements.txt` (don't
   copy `venv/` — it's tied to the old machine's Python build).
3. Copy the git-ignored secrets/state from the old server: `.env`,
   `google_credentials.json`, `bot_state.db` (optional — carries over
   polling cursors and same-day dedup state; safe to skip, cursors
   re-bootstrap to "now" on first run so you won't get a history replay).
4. Copy the systemd unit, update `WorkingDirectory`/`ExecStart` paths, then
   `systemctl daemon-reload && systemctl enable --now <service>`.
5. Verify: `journalctl -u <service> -f` — look for `Odoo connection OK
   (uid=...)` and `Scheduler started` with no tracebacks.
6. Same `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` work from any host — no
   Slack-side reconfiguration needed for a same-app migration.
