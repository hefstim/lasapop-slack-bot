# Odoo ↔ Slack 机器人（中文版）

> [English version](README.md)

一套代码，两个独立运行的实际部署——HEFS（菲律宾）和 LasaPop。两边都是把 Odoo（JSON-2 API）和 Slack 打通：定时轮询 Odoo、往 Slack 推通知/交互卡片、让员工一键操作、导出数据到 Google Sheets。每个部署有自己独立的 systemd 服务、独立的 `.env`、独立的 `bot_state.db`，功能上也已经互相分叉——**但两个目录是同一个 GitHub 仓库的 git checkout**，做任何 git 操作前务必先看下面"⚠️ 共用 git remote"那一节。

| | HEFS | LasaPop |
|---|---|---|
| 路径 | `/opt/odoo-slack-bot`（服务器 `japan_oracle`，150.230.63.12） | `/opt/lasapop-slack-bot`（同一台服务器） |
| systemd 服务 | `odoo-slack-bot.service` | `lasapop-slack-bot.service` |
| Slack 身份 | `@odoobot`，HEFS 工作区 | `@lasapop_bot`，LasaPop 工作区 |
| Odoo | `https://www.02ph.com`，db 见 `.env` | `http://localhost:8069`，db 名 `lasapop` |
| 运营状态 | 完全在线——真实的订单/付款/发票流水 | **业务尚未正式运营**——`main.py` 里大部分 Odoo 同步任务都被禁用（见下文）；报销审批与备用金（2026-10-19 起报销从备用金入账）在用 |

## 共享架构

一个长期运行的 Python 进程（`main.py`）同时做两件事：

- **APScheduler** —— `scheduler/jobs.py` 里的后台任务，按 cron/interval 调度。
- **Slack Bolt（Socket Mode）** —— `slack/handlers.py` 里的 `@mention` 命令 + 按钮点击，实时响应。

```
main.py                  入口 —— 构建调度器、启动 Slack Socket Mode
config.py                所有配置，从 .env 读取
db.py                    SQLite（bot_state.db）—— 轮询游标、提醒/去重状态、
                          数据库损坏自愈逻辑（见下方"已知问题"）
odoo/
  client.py              基于 JSON-2 API 的 Odoo 客户端（bearer API key）。读操作遇网络错误重试一次。
  json2.py               仅用标准库的 JSON-2 客户端；execute_kw() 保持 XML-RPC 的调用写法。
  orders.py / payments.py / invoices.py / quotations.py / inventory.py
  customers.py / reports.py / integrity.py
  expenses.py             仅 LasaPop —— hr.expense 提交/审批/拒绝、重复确认、备用金类别/入账/余额
scheduler/
  jobs.py                 每个定时任务一个函数 —— 定时任务调用 Slack
                           chat.postMessage 的唯一入口
  cash_jobs.py             仅 LasaPop —— 现金结余核对（代码已写好，但目前
                           并未接入 main.py 的调度器，见下文）
slack/
  app.py                  Slack Bolt App 初始化 + Socket Mode handler
  handlers.py              @mention 命令 + 按钮/shortcut/modal 处理
  messages.py               Block Kit / 文本消息构建
google_sheets/
  exporter.py / invoice_exporter.py / contact_exporter.py / cash_reader.py（LasaPop）
```

> **死代码提醒：** 两个部署的**仓库根目录**下也都各自躺着一份
> `handlers.py`、`messages.py`、`db.py`、`jobs.py`、`customers.py`、
> `reports.py`——这些是重构成包结构之前留下的旧文件，`main.py` 并不会
> import 它们。不确定哪份文件是真正生效的，就去看 `main.py` 的 import，
> 别看根目录。

### `bot_state.db`（SQLite，git 不跟踪，每个部署各自一份）

| 表 | 用途 |
|---|---|
| `notification_state` | key/value 轮询游标：`last_order_id`、`last_payment_ts`、`integrity_last_check_ts`、`last_customer_check_ts` |
| `customer_reminders`、`invoice_reminders`、`quotation_reminders`、`stock_alerts` | 每条提醒卡片一行 —— 支撑交互按钮 + 同日去重 |
| `excluded_customers` | "不再购买"黑名单 |
| `integrity_alerted_orders` | 30 天未开票检查已经提醒过的订单 |
| `expense_requests`、`cash_topups` | 仅 LasaPop —— 报销审批（含卡片位置）+ 现金充值记录（备用金开启后不再写入，余额以 Odoo 为准） |

滚动备份存在 `backups/` 目录，HEFS 每小时一份，LasaPop 每 15 分钟一份。

## HEFS —— 当前生效的任务（`scheduler/jobs.py`，全部启用）

| 任务 | 调度 | 推送到 |
|---|---|---|
| `job_new_orders`、`job_new_payments`、`job_new_customer_contact_check` | 每 5 分钟，08–19 点 | sales / finance / odoo-report |
| `job_integrity_check` | 每小时 | odoo-report |
| `job_reactivation_check` | 每 4 小时 | management |
| `job_sync_wont_buy` | 每 2 小时 | —（不发消息） |
| `job_low_stock` | 每天 08:00 | warehouse |
| `job_dormant_customers` | 每天 09:00 | sales |
| `job_overdue_invoices` / `job_export_overdue_invoices` | 每天 09:30 / 09:35 | finance |
| `job_pending_quotations` | 每天 10:00 | sales |
| `job_daily_report` | 每天 18:00 | management |
| `job_weekly_report` | 周一 08:30 | management |
| `job_export_legacy_invoices` | 周一 09:45 | finance |
| `job_monthly_report` | 每月 1 号 09:00 | management |

**频道坑点：** HEFS 的 `.env` 里，`SLACK_CHANNEL_SALES` = `SLACK_CHANNEL_FINANCE`
= `SLACK_CHANNEL_WAREHOUSE` = `SLACK_CHANNEL_MANAGEMENT`，全都是字面量频道名
`odoo-report`——也就是说**所有任务实际上都推到同一个频道**"odoo-report"
（频道 ID `C0BAQCGCY8L`，运行时按名称解析）。另外还有一个**单独的**配置项
`SLACK_CHANNEL_ODOO_REPORT`，配的是**另一个不同的**频道 ID
（`C0ARTKSPG81`），只被 `job_integrity_check` 和
`job_new_customer_contact_check` 使用。不要想当然认为"odoo-report"就是指
一个频道——具体要看某个任务实际读的是哪个配置项。

`@mention` 命令：`report`/`daily`、`weekly`、`monthly [YYYY-MM]`、
`thismonth`、`dormant`/`customers`、`invoices`、`invoicesheet`、`legacy`、
`quotations`/`quotes`、`stock`、`help`。

## LasaPop —— 哪些活着，哪些停用

**目前只有 `job_integrity_check`（每小时）和 `db.backup_db`（每 15 分钟）
是启用状态。** 共享 `scheduler/jobs.py` 列表里的其他所有任务——新订单/
新付款、各种报表、沉睡客户、逾期发票、低库存、客户重新激活、Won't-Buy
同步——都在 `main.py` 里被 `# [停用-未运营]` 注释掉了。这是故意的：
LasaPop 的业务目前还没有真实订单流水跑进 Odoo（2026-08-11 确认：
`sale.order` 记录数为 0）。**如果发现 LasaPop 这边 `job_new_orders`
"不工作"，先去看 `main.py`——这是设计上的停用，不是故障。**

另外还有一整套现金核对子系统（`scheduler/cash_jobs.py`、
`google_sheets/cash_reader.py` —— `job_cash_balance_verify`，每晚 Odoo
收款 vs Google Sheet 记录的双重核对）代码已经写好，但**目前根本没有
`add_job()` 调用把它接入调度器**（连注释掉的痕迹都没有，就是从没加进去
过）。已经造好了，但还没上线。

### 真正在跑的：报销审批 + 备用金（Slack 原生，不依赖 Odoo 订单）

> **2026-10-19 更新：** 报销改为从工厂备用金逐笔入账（`PETTY_CASH_ENABLED=true`），备用金充值改由 AI
> 助手记录。此前"不进入正式记账、每月整笔记账"的设计已废止。员工 / 管理层手册见
> GitHub `hefstim/hermes-odoo` 的 `docs/manuals/`，Odoo 侧设置见同仓库 `docs/petty-cash/`。

`@mention` 命令：`expense <金额> <说明>`、`expensetotal`、`cashin <金额> <备注>`（仅管理员，
见 `Config.ADMIN_USER_IDS`）、`balance`、`help`。同样的操作也能通过 Slack shortcut（⚡ 图标）触发：
*提交支出*、*查询现金统计*（结果发在 `#exp-approval`，不是私信）、*充值现金*。

**提交：** `expense` 提及或 *提交支出* 表单 → `odoo/expenses.py::create_and_submit_expense` 创建并提交
`hr.expense` → 审批卡片发到 `SLACK_CHANNEL_EXPENSE_APPROVAL`（`#exp-approval`）。附带的图片作为凭证存成
`ir.attachment`（`attach_receipt`），上传失败只记日志，不阻塞提交。`Config.EXPENSE_EMPLOYEE_MAP`（`.env` 里
`Slack用户ID=Odoo员工ID`）是 Slack 到 Odoo 员工身份映射的唯一来源；不在表里的提交人会被友好拒绝。

**审批：** 管理员点 Approve/Refuse → `slack/handlers.py::_update_expense`（先原子地 claim 本地
`expense_requests` 行，防双击）→ `approve_expense` / `refuse_expense`（拒绝走
`hr.expense.refuse.wizard`；直接调 `action_refuse()` 只会弹 UI 对话框）。

- **可能重复的报销：** 同一员工、同一天、同金额时，Odoo 的 `action_approve` **不报错**，而是返回
  `hr.expense.approve.duplicate` 向导，报销仍停在 submitted。2026-10-19 之前机器人把这种情况当成功，
  卡片显示 Approved 而 Odoo 没批（报销 12、17，已手工补批）。现在 `approve_expense` 抛
  `DuplicateExpenseError`，`_ask_duplicate_confirmation` 私下给审批人发提示和
  "Not a duplicate — approve / Cancel" 两个按钮（`expense_approve_duplicate` / `expense_duplicate_cancel`）；
  确认后经向导只批准这一笔。最后一定回读 Odoo 状态，不符就报错并释放 claim，允许重试。
- **没有报销审批人的员工**（`hr.employee.expense_manager_id` 为空）提交即被 Odoo 自动批准，并跳过重复检查
  （`hr.expense._can_be_autovalidated`）。新员工务必设置审批人；2026-10-19 已为 Wei Liu 设为 hellolee120。

**备用金（`Config.PETTY_CASH_ENABLED`，2026-10-19 起为 true）：**

| | 开关打开 | 开关关闭（旧流程） |
|---|---|---|
| 报销付款方式 | `company_account`，从 Odoo 账簿 `PCSH 工厂备用金` 支付 | `own_account`（员工垫付） |
| 类别 | 表单必选；卡片上有下拉框（block `expense_category` / action `expense_category_select`），**未选不能批准**；`set_expense_category` 写入产品 | 统一用默认产品 |
| 批准后 | `action_post` → 状态 paid，分录 借 类别科目 / 贷 101101 | 停在 approved，不入账 |
| `balance` / 统计 | `get_petty_cash_balance()`：Odoo 101101 已过账余额；`expensetotal` 加按类别汇总 | 本地 `cash_topups` − 已批准合计 |
| `cashin` / *充值现金* | 不记账，只提示改用 AI 助手（`record_petty_cash_topup`） | 写入本地 `cash_topups` |

类别在 `odoo/expenses.py::EXPENSE_CATEGORIES`，按 Odoo 产品的内部参考 `EXP-*` 查找；各类别对应的科目
（运费 614000 … 设备 151100、装修 151200 为固定资产）在 Odoo 产品上设置，由 `petty_cash_setup.py` 创建。
期初余额 MISC/2026/10/0023（借 101101 / 贷 600000 PHP 31,868）。关闭开关即恢复旧流程，不需改代码。

## ⚠️ 共用 git remote —— 做任何 git 操作前先看这里

`/opt/odoo-slack-bot` 和 `/opt/lasapop-slack-bot` 都是**同一个仓库**
`git@github.com:hefstim/odoo-slack-bot.git` 的 git clone，都在 `main`
分支上。LasaPop 是从 HEFS 的代码起步的，后来通过**大量从未提交、从未推送
的本地修改**（报销/现金功能、停用的任务、不同的配置）分叉出去——所以在
任意一个目录里 `git status` 几乎总能看到一堆跟你当前要做的事情无关的、
早就存在的已修改/未跟踪文件。提交或推送之前：

1. **先看 `git status`，不要无脑 `git add -A`**——某个文件里可能已经混着
   历史遗留的改动和你刚做的改动。遇到这种情况，用
   `git diff > /tmp/x.patch` 把完整 diff 导出来，手工把 patch 精简到只剩
   你自己的那部分改动，再用 `git apply --cached` 应用——不要把整个文件
   的 diff 一起提交。
2. **绝不提交 `.env.bak-*` 文件**（LasaPop 那边就有好几个未跟踪的，
   大概率含密钥）。
3. **从一边 push 之后，另一边的 `origin/main` 就落后了。** HEFS push 完
   之后，LasaPop 本地 `main` 需要先
   `git fetch && git rebase --autostash origin/main` 才能再 push（反过来
   也一样）。`--autostash` 很关键——两边目录都有必须在 rebase 过程中
   保留下来的未提交本地改动。
4. **如果同一个逻辑修复被分别搬到了两边**，rebase 时可能会**自动丢弃你
   的 commit**（Git 识别出这个 patch 内容已经在上游了——不需要再 push，
   已经是最新的了），**也可能冲突**（如果两边版本有细微差异，比如一边
   多了线程安全或额外功能代码）。出现冲突要手工解决——尤其是"旧版本一侧
   为空"这种冲突块，千万别信任脚本化/正则表达式的自动解决方式；一个偷懒
   的正则表达式可能会把夹在两个冲突标记之间的正常代码给悄悄吞掉。正确
   做法是把冲突文件下载下来，一块一块对照着人工解决，验证
   `python3 -m py_compile` 通过、没有重复的 `def`，再继续 rebase。

## 已知问题 / 排错手册

**"`job_new_orders` 老是被跳过"（`journalctl` 里看到
`Execution of job "job_new_orders..." skipped: maximum number of running
instances reached (1)`）**—— 说明这个任务卡住了，不是空闲。用
`journalctl -u <service> --since <该任务最后一条 "Running job" 的时间>`
看它到底卡在哪。最常见的原因见下面。

**数据库损坏导致游标重置 → 全量历史订单刷屏（2026-08-11 定位，HEFS）：**
`db.py` 的 `_check_db_file()` 会自愈损坏的 `bot_state.db`（SQLite 文件头
不合法）——备份成
`bot_state.db.corrupted_<时间戳>`，重建一份空 schema。这个检查在每次数据
库连接前都会触发，不只是启动时，所以损坏可能发生在运行过程中。除了
`last_order_id`，其他所有游标在各自的 `get_state(key, now)` 调用点都是安
全地默认成"现在"，所以就算被重置，最多也就是"今天的东西可能重复提醒一
次"。**唯独 `last_order_id` 默认值是 `"0"`**——所以游标一旦丢失，
`job_new_orders` 就会把*有史以来所有*已确认订单都当成"新订单"抓出来，逐
条推到 Slack，每条间隔 1.2 秒。HEFS 这边因此把约 808 条历史订单在 19
分钟内刷进了大家共用的 "odoo-report" 频道，直到手动 `systemctl restart`
才停下来（重启之所以能解决，是因为 `main()` 自己启动时的 bootstrap 逻辑
会把 `last_order_id` 重新设成当前最大值——bug 出在
`_check_db_file()` 的原地自愈逻辑没有做同样的事）。**两边部署都已修
复：** `_check_db_file()` 现在会用它手头已经打开的那个连接，自己把
`last_order_id` 重新初始化——而不是通过 `set_state()`，因为
`set_state()` 会走 `get_conn()` → `_check_db_file()` → 同一把非可重入的
`_check_lock`，会把整个进程死锁。以后如果再改动 `_check_db_file()`，任
何游标修复逻辑都要用它已经打开的那个 `conn`，绝对不要在里面调用
`set_state()`/`get_conn()`。

**`app_mention` 触发不稳定**——经常连续很长时间一次都不触发，但触发的
时候又可能对同一条消息重复投递两次（实际观测到过：一次 `expense`
提及创建了两条 `hr.expense` 记录）。两边部署都已修复：把
`handle_mention` 的处理逻辑挪进共用的 `_dispatch_mention()`，同时挂在
`app_mention` 和一个 `message` 事件的兜底逻辑上（检查文本里是否含
`<@bot_user_id>`），并加了 `(channel, ts)` 去重锁，不管到底是哪个/哪些
事件触发、触发几次，每条真实消息只会被处理一次。

**仅 LasaPop —— `<@BOTID>` 形式的提及会静默丢失：** 用户复制粘贴之前的
消息而不是重新打 `@` 从自动补全里选，粘贴的文本可能带的是应用的
`bot_id`（`<@BOTID>` 形式），而不是 `bot_user_id`（`<@USERID>` 形式）。
上面那个兜底逻辑原本只检查 `bot_user_id`，所以这种消息在
`app_mention` 和兜底逻辑里都匹配不上，悄无声息地消失，日志里连痕迹都没
有。已修复：`_get_bot_id()` 缓存 `auth_test()` 返回的 `bot_id`，兜底逻辑
现在两种形式都认。

**通用函数 `update_status(table, row_id, status, note="")` 要求表里必须有
`note` 列**——它无条件执行 `SET status=?, note=?, ...`。任何新接入这个函数
的 SQLite 表（除了那四张提醒/告警表，还有 `expense_requests`）都必须有
`note TEXT` 这一列，否则该表上的每次按钮点击都会在
`chat_update` 还没执行到之前就静默失败（2026-08-04 修复，当时
`invoice_reminders`/`quotation_reminders`/`stock_alerts` 缺这一列已经缺了
很久了——同样的 bug 在 `expense_requests` 上因为还在测试阶段而被立刻抓
到）。

**2026-10-17 改用 JSON-2 API 后已解决**——返回 `None` 的方法现在正常返回 `null`，`_safe_execute()` 已删除。
以下为历史记录，保留是因为“写入后单独回读确认”的建议仍然适用：

**两套 Odoo 的 XML-RPC 在调用返回 `None` 的 action 方法时都会抛出一个假的
`cannot marshal None` 错误**——`TypeError: cannot marshal None unless
allow_none is enabled`，是服务端在序列化 XML-RPC 响应的时候抛出来的，
这时候底层的写入其实**已经提交成功了**。LasaPop 这边确认过
（`hr.expense` 的 `action_submit`、`action_approve`——
`odoo/expenses.py::_safe_execute()` 专门吞掉这个特定的错误文本），
HEFS 这边也独立确认过同样的现象（`stock.quant.action_apply_inventory`，
2026-08-15 批量做库存盘点调整时——42 条里每一条调用都抛了这个错，如果
脚本把异常当成失败来计数，会显示"0 条成功"，但实际上写入全都生效了）。
**永远不要相信脚本自己用 try/except 统计出来的成功/失败数**——调用这类
action 方法之后，一定要单独重新读取一次记录，确认真实状态，跟处理任何
"发了就不管"的写入操作一样。以后对这两个 Odoo 实例新增任何 action 方法
调用，都要预料到同样的现象。

**通过 API 做库存调整（`stock.quant`）时——新建一条从未存在过的
quant，如果在 `create()` 调用里直接把 `inventory_quantity` 一起传进去，
会静默不生效**，即便 `create()` 本身正常返回了一个 id、没有报任何错。
之后再对这个新 quant 调 `action_apply_inventory`，库存的 `quantity`
字段也不会变。2026-08-15 核对 HEFS 的 Antipolo 仓库实盘数据时确认过：
42 个产品里，40 个原本就有 quant 记录的，用
`write({'inventory_quantity': N})` + `action_apply_inventory` 都正确生效
了；另外 2 个该库位之前完全没有 quant 记录、靠 `create()` 新建的，即便
创建时就带上了 `inventory_quantity`，库存还是静默停在 0。解决办法：
`create()` 的时候不要带 `inventory_quantity`（只传
`{'product_id', 'location_id'}` 就行），**单独**再调一次 `write()` 设置
`inventory_quantity`，最后再 `action_apply_inventory`——跟更新已有 quant
的两步流程一样，只是前面多一步 `create()`。

**`SLACK_CHANNEL_ODOO_REPORT` 是一个私有频道**——必须用频道 **ID**
配置，不能用名称；bot 的 token 没有 `groups:read` 权限，没法自己把私有
频道名解析成 ID。

**删除 Slack 消息不会撤回已经推送到手机上的通知。** 如果刷屏消息已经推
到了大家手机上，频道里的消息本体可以用 `chat.delete` 批量清理（需要
`channels:history`/`groups:history` 这个 bot 权限，默认 token 没有——去
Slack app 的 OAuth & Permissions 页面加上并重新安装到工作区；重新安装不
一定会换新 token，跟 `.env` 里现有的比对一下就知道），但已经推送到手机
上的通知已经从 Slack 这边发出去了，只能在手机客户端里手动清。

## 服务器上的其他服务（japan_oracle）

不属于这个仓库，但因为跑在同一台服务器上、容易跟这个项目的问题搞混，
值得记一下：

**`hermes-agent`**（NousResearch 出品，装在 `/usr/local/lib/hermes-agent`，
第三方代码，不是我们自己写的）——一个独立的 AI agent 消息网关，两个
profile/systemd 服务：`hermes-gateway.service`（HEFS，
`@geminicloudrunbot`）和 `hermes-gateway-lasapop.service`（LasaPop）。
2026-08-19 这天两边同时踩到了两个 bug：

- **Slack Socket Mode 会卡进一个不会自愈的重连死循环。** 传输层断开之后，
  重连逻辑复用了一个已经关闭的 `aiohttp.ClientSession`，而不是新建一个，
  所以每次重试都立刻报 `RuntimeError: Session is closed`，然后就再也没法
  自己恢复——`journalctl` 里这一行会每隔几秒刷一次，无限刷下去。HEFS 卡了
  大约 1.5 小时才被发现；LasaPop 从 **2026-08-10 就开始卡住，卡了差不多
  9 天，累计报错 23.5 万多次**——之所以一直没人发现，是因为单独的一次性
  `hermes send` 命令行路径（`hermes-fallback-watch.service` 发告警用的
  就是这个）不依赖那个已经坏掉的常驻 socket，所以对外发告警一直正常，
  但接收 `@mention` 消息的监听其实已经彻底死了。**症状：** 进程本身
  `systemctl status` 显示 `active (running)`，看起来很健康，但机器人对
  任何消息都没有反应。**处理方法：** 先用 `journalctl -u <服务名> | grep
  -c "Session is closed"` 确认，然后直接 `systemctl restart <服务名>`
  就行——不需要改任何配置，一个全新的进程会拿到一个全新的 session。上游
  没有出过正式修复，以后再遇到还是重启这个临时办法。
- **LasaPop 的 `openai-codex` 凭证池里的两个条目其实是同一个 ChatGPT
  账号**，不是真正的两个不同账号。对比一下 HEFS 的
  `/root/.hermes/auth.json`（`openai-codex-oauth-1` = qilin0369@gmail.com，
  `openai-codex-oauth-2` = fengjunhua113@gmail.com——是真的两个不同账号，
  所以一个用完周额度另一个还有）和 LasaPop 的
  `/root/.hermes/profiles/lasapop/auth.json`，会发现里面两个凭证池条目
  （`device_code` 和 `openai-codex-oauth-2`）解码出来的
  `access_token`/`refresh_token`/`chatgpt_account_id` 完全一样（都是
  qilin0369@gmail.com）。所以 LasaPop 这边根本没有真正的故障转移——那一个
  账号一旦 `usage_limit_reached`，两个凭证池条目会同时显示
  `exhausted`，所有请求直接落到 deepseek/openrouter 兜底。要修的话需要
  用**另一个**账号重新走一遍 `hermes -p lasapop auth add openai-codex
  --type oauth` 的交互式扫码登录（没法非交互式完成）——而且按前面那条
  "hermes 已知 bug" 提到的，加完凭证之后要重新检查一下 `config.yaml` 里
  的 `model:` 块有没有被搞坏。

## 常用排查命令

```bash
# 服务健康状态
sudo systemctl status odoo-slack-bot.service        # 或 lasapop-slack-bot.service
sudo journalctl -u odoo-slack-bot.service -f

# 任务是不是卡住了？（对比 "Running job" 和下一条 "executed successfully"）
sudo journalctl -u odoo-slack-bot.service --no-pager | grep -i job_new_orders | tail -20

# 轮询游标
sqlite3 /opt/odoo-slack-bot/bot_state.db "select * from notification_state;"

# 数据库自愈机制有没有触发过？
ls -la /opt/odoo-slack-bot/bot_state.db.corrupted_*
```

## 迁移 / 交接

1. `git clone git@github.com:hefstim/odoo-slack-bot.git`（先看上面共用
   remote 那节的提醒——清楚自己带过来的是哪个部署的本地分叉，因为全新
   clone 出来的代码既没有 HEFS 也没有 LasaPop 各自的未提交内容）。
2. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
   （不要直接拷贝 `venv/`——它跟旧机器的 Python 构建版本绑定）。
3. 把 git 不跟踪的密钥/状态从旧服务器拷过来：`.env`、
   `google_credentials.json`、`bot_state.db`（可选——带过来能保留轮询游
   标和同日去重状态；不带也没关系，游标首次运行会自动初始化成"现在"，
   不会导致历史全量重放）。
4. 拷贝 systemd unit 文件，更新其中的 `WorkingDirectory`/`ExecStart`
   路径，然后 `systemctl daemon-reload && systemctl enable --now <服务名>`。
5. 验证：`journalctl -u <服务名> -f`——确认能看到
   `Odoo connection OK (uid=...)` 和 `Scheduler started`，没有报错堆栈。
6. `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` 在任何服务器上都能用——同一个
   Slack app 迁移不需要在 Slack 那边重新配置。
