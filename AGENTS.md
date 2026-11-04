# Glow-worm: Project Context & Guidelines

This file provides guidance to AI coding assistants (Claude, Grok, Cursor, etc.) when working with code in this repository.

## Project overview

A single-tenant household budgeting app. This is a Python/FastAPI project using: SQLAlchemy + SQLite/PostgreSQL, Alembic migrations, Jinja2 templates, Pydantic schemas, uv for dependency management. Always use `uv run` to execute commands (e.g., `uv run pytest`, `uv run alembic`).

## Common Commands
- **Install Dependencies**: `uv sync`
- **Run Application**: `uv run uvicorn app.main:app --reload`
- **Database Migrations**: `uv run alembic upgrade head`
- **Create Initial User**: `uv run python scripts/create_user.py`
- **Repair stored budget spent (dry run)**: `uv run python scripts/reconcile_budget_spent.py` (`--apply` writes). In Docker: `docker exec -it <container-id> python scripts/reconcile_budget_spent.py`
- **Run All Tests**: `uv run pytest`
- **Run Specific Test**: `uv run pytest tests/test_filename.py`
- **Coverage Report**: `uv run pytest --cov=app --cov-report=html`
- **Type check**: `uv run pyrefly check app/`
- **Lint**: `uv run ruff check .`
- **Format check**: `uv run ruff format --check .`
- **Format fix**: `uv run ruff format .`
- **Install git hooks**: `uv run prek install`
- **Run git hooks**: `uv run prek run --all-files`
- **Update secrets baseline**: `uv run detect-secrets scan > .secrets.baseline`
- **Build container**: `container build --tag test --file Dockerfile .`
- **Run container**: `container run --name test --rm -e SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))") test`
- **Run CLI (dev)**: `uv run glow --help`
- **Install CLI via pipx**: `pipx install .`
- **Run process income allocation manually**: `uv run python -c "from app.tasks import process_income_allocation; process_income_allocation()"`
- **Run share price and FX update manually**: `uv run python -c "from app.tasks import process_share_market_data; process_share_market_data()"`

## Architecture & Money Flow
The app manages four distinct, separated money systems, plus an informational shares ledger that does not post household transactions:
1. **Income Allocation**: Automated distribution on the 1st of the month based on `IncomeAllocation` config.
2. **Monthly Budget**: Repeating monthly categories (Groceries, etc.). Funded via income; tracks `spent_amount` vs `allocated_amount`.
3. **Sinking Funds**: Savings pots (Bills, Savings, etc.) with `current_balance`.
4. **Recurring Bills**: Managed within the "Bills" Sinking Fund. Each bill can name the payment method that pays it (card, bank account, or BPAY / pay myself). Only the last 4 digits are stored.
5. **Shares** (informational): US share trades, FIFO cost base, and daily prices. Stored on `share_trades`, `share_prices`, `fx_rates`, and `market_data_runs`. Not linked to `Transaction`, budgets, or sinking funds.

## Code Style & Standards
- **Backend**: Python 3.14+, FastAPI (async routes), Pydantic (validation), SQLAlchemy (ORM).
- **Frontend**: Jinja2 templates + HTMX for SPA-like feel. Tailwind CSS via CDN.
- **Database**: SQLite (default/dev) or PostgreSQL (production). Set via `DATABASE_URL` env var. PostgreSQL uses psycopg v3 (`psycopg[binary]`), which SQLAlchemy 2.1 selects for a `postgresql://` URL. Alembic imports the engine at startup and that dialect imports `psycopg` immediately. Use **Soft Deletes** (`is_deleted=True`) for Categories and SinkingFunds to preserve history. System categories (`is_system=True`) cannot be deleted — these are required for income allocation (the first `income`-type and the `transfer`-type category) and bill tracking (the `Bills` expense category).
- **Typing**: Use PEP 604 union syntax — `str | None`, `int | str`, etc. Do **not** use `Optional[T]`, `Union[T, None]`, or `Union[...]` from `typing`. Prefer built-in generics (`list[str]`, `dict[str, int]`) over `typing.List` / `typing.Dict`.
- **Dates**: Store as ISO 8601 strings (`YYYY-MM-DD`). Use `pytz` for timezone handling (`Australia/Brisbane`).
- **TDD**: Write tests in `tests/` before implementation. Aim for >80% coverage.

## Middleware Stack
Middleware execution order (outermost to innermost): CORS (optional) → Session → CSRF → Authentication.
- **CSRF Exemptions**: `/login`, `/logout`, `/api/*` (Bearer token auth), `/mcp` (MCP protocol).
- **Session**: 7-day expiry (`max_age=604800`).

## Security & Safety
- **Dual Authentication**:
    - **Session-based** (web UI): `Starlette SessionMiddleware`. All routes except `/login` require auth.
    - **Bearer token** (API/MCP): `Authorization: Bearer <token>` header. API keys are SHA-256 hashed (high-entropy tokens, not passwords). Checked before session auth in middleware.
- **API Keys**: Stored in `api_keys` table. Rate limited to 5 active keys per user, 1 new key per 24 hours. Revoked keys don't count toward active limit.
- **Session Versioning**: `User.session_version` increments on password change, invalidating all existing sessions.
- **Passwords**: Hashed with `passlib` (bcrypt). Minimum 8 characters.
- **CSRF**: `starlette-csrf` middleware required. All HTMX non-GET requests must include `X-CSRF-Token`.
- **Transactions**:
    - Support **Dual-Linkage**: A transaction can have both a `sinking_fund_id` and a `recurring_bill_id` (e.g., paying a bill from a fund).
    - Transaction types: `regular`, `income`, `income_allocation`, `secondary_income_allocation`, `contribution`, `withdrawal`, `budget_expense`, `budget_transfer`. (`secondary_income_allocation` is for goal-fund contributions from secondary income, kept distinct from `income_allocation` so monthly goal-progress tracking is not polluted by primary allocations.)

## Logic Specifics
- **Bills Allocation**: Recommended = (Total Annual Bill Cost / 12).
- **Bills forecast**: `generate_bills_forecast` (page `/sinking-funds/{id}/forecast` only; no JSON or MCP consumer) projects active bills with `iter_bill_due_dates`. `contribution` is money still to come and the only primary contribution added into `closing_balance`, which starts from the fund's `current_balance`. If this month already has any `income_allocation` on the Bills fund, the first row keeps `contribution` at 0, sets `contribution_received` True, and sets `received_contribution` to the sum of those deposits. Later rows keep the flag False. The page prints that sum with a "received" note. The Monthly Contribution summary uses `configured_contribution` (fixed amount, or recommended annual cost / 12) rather than the first row's still-to-come figure. Bills cover does not read these rows. `include_secondary` (page query `?secondary=1` only; default off; not stored) adds each row's `secondary` = max(0, Bills secondary goal − `secondary_income_allocation` recorded on that fund in that row's month), including future-dated rows, into `closing_balance`. `secondary_received` is that recorded sum for display and is not added again. `income_allocation` rows are not secondary progress. The goal is the live sum from `app/services/bills_secondary.py` for the same non-deleted fund named Bills. If that fund is missing or the goal is 0, the toggle is hidden and nothing is added. A mid-month goal edit does not claw back `current_balance`. The HTMX swap replaces the summary bar (including 12-Month Low) and the table. Secondary is its own column (`+$X (secondary)`, plus `$Y recorded` when that month has a recording) and is omitted while the toggle is off. The primary-deposit check is unchanged. Bills cover still subtracts recorded secondary for the current month only.
- **Bills cover (dashboard)**: `app/services/bills_cover.py` looks ahead `BILLS_COVER_HORIZON_MONTHS` calendar months (default 3, clamp 1–12; end date is today plus that many months). Obligations are active recurring-bill occurrences from `next_due_date` through the horizon — including an overdue `next_due_date` — walked with `iter_bill_due_dates` / `advance_due_date` (same frequency rules as `generate_bills_forecast`), plus unpaid `expense` transactions with `recurring_bill_id` set, `date <= horizon end` (past dates still count), and either no sinking fund or the Bills fund. The same bill and due date is counted once, using the unpaid ledger amount. Scheduler bill rows are `is_paid=True` by default and already reduce the Bills balance, so they are not unpaid obligations. Without-secondary cash is the Bills balance plus remaining primary Bills contributions: this month's amount if no `income_allocation` to Bills has posted yet, plus each later 1st on or before the horizon end. With-secondary cash adds each intersecting month's Bills secondary `goal_amount`, minus `secondary_income_allocation` already recorded to Bills in the current month only (future months use the full goal). The dashboard banner is shown only when the without-secondary path is short, and then prints both figures. The sinking-funds list no longer has the 30-day buffer warning. Bills cover does not replace `/sinking-funds/{id}/forecast`.
- **Fund runway**: `app/services/fund_runway.py`, shown only on the sinking-fund detail page (above the month navigation, as of today, ignoring `?month=&year=`). Net rate is `Transaction.type` only (`income`/`transfer` in, anything else out) for that fund's rows over the last 6 complete calendar months; the current month is excluded and a month with no rows counts as zero. History starts at the earlier of `created_at` (naive values treated as UTC, then `Australia/Brisbane`) and the earliest transaction date; the first counted month is the first full month on or after that date (a fund created on the 1st counts that month). Fewer than 2 months used shows no number. Balance > 0 and net burn > 0 shows whole months rounded down, a run-out month, a warning under 6 months, "less than a month" under 1, and "more than 10 years" over 120. Balance 0 and still burning is an empty warning. Balance < 0 is a debt: net inflow shows a payoff rounded up on a neutral card (same 120-month cap); otherwise a warning that it isn't being paid down. The Bills fund shows a Forecast pointer and no figure. Soft-deleted funds render nothing. Uses the stored `current_balance` (MCP balance drift is unchanged). No new tables, migrations, or env vars.
- **Budget Funding**: The "Monthly Budget Allocation" is the **sum** of all individual category `allocated_amount` targets.
- **Budget Recommendations**: Advisory only (no auto-apply). `app/services/budget_recommendations.py` compares each current-month budget's `allocated_amount` to the **average** of `spent_amount` over up to the last **6 completed months** (min 2 months of history). Emits raise/lower suggestions only when `|delta| >= max($20, 10% of allocated)`. Shown on `/budgets` (current month panel) and as a dashboard teaser. Uses `Budget` history rows, not raw transactions.
- **Recurring Transfers**: Defined on `IncomeAllocation` via the `IncomeAllocationRecurringTransfer` model. Each has a `description` and `amount`. During `process_income_allocation()`, one `expense`/`income_allocation` transaction is created per transfer (using the transfer category). The amount is deducted from `total_allocated`, reducing the unallocated remainder. No sinking fund balance is updated — the money leaves the household budget entirely. Configured via the `/income` page.
- **Scheduler**: Use `APScheduler`. Handle Leap Years by defaulting to the last day of the month for invalid February dates (e.g., Feb 29th -> Feb 28th). Jobs: income allocation, bill processing, and `share_market_data` (default 18:00 `America/New_York`, Mon–Fri, `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`). When `SHARES_CATCHUP_ON_STARTUP` is true, a one-shot catch-up is scheduled 60 seconds after startup if the newest market-data run is older than 20 hours (or there is no run) and there is an open holding or no stored AUD/USD row.
- **Shares**: FIFO only, ordered by `(trade_date, id)`. Cost base and proceeds for USD trades are converted with the broker FX rate stored as AUD per USD. Frankfurter is the primary daily AUD/USD source; Alpha Vantage `FX_DAILY` is the fallback. US prices use Alpha Vantage `TIME_SERIES_DAILY` when `ALPHA_VANTAGE_API_KEY` is set. The key is redacted from logs and run messages. A missing key skips prices and still fetches FX. Failed price calls keep the last good close and its `as of` date. Hard-deleting a buy that would make a later sell an oversell is rejected. `.AX` / `.AUS` tickers are accepted as AUD instruments (no FX, fee currency forced to AUD) so the provider interface can grow; v1 does not price ASX symbols. `/shares` charts portfolio market value and FIFO cost base in AUD (`build_portfolio_series`). A point is drawn only when every open holding has a stored close on or before that day, carrying the last close and AUD/USD rate across gaps. The 1M/3M/6M/1Y/All toggle filters that series in the browser with the same calendar-month window as `filter_portfolio_series`. The chart reuses D3, already loaded from `d3js.org` for the income Sankey. Fewer than two valued days shows an empty state.
- **Transaction balances**: `app/services/transaction_balances.py` is the only place that applies a transaction to `SinkingFund.current_balance`, `Budget.fund_balance`, and `Budget.spent_amount`. The transactions HTML routes, the JSON API, and the MCP transaction tools all use it. The dashboard quick-expense form calls the same spent helper. `type` `income` or `transfer` adds the amount to the linked fund; any other `type` subtracts. `is_paid` and `transaction_type` do not change the sinking-fund balance. `budget_transfer` adds the amount to `Budget.fund_balance`; that effect is reversed on delete and when the amount, transaction type, budget, or fund link changes. `spent_amount` changes only for an expense whose `transaction_type` is `budget_expense` and whose `budget_id` is set (the same rows quick-expense creates). The amount is added on create, subtracted on delete, and on edit the net change is applied once: amount edits, a move between budgets, clearing or setting `budget_id`, and a type or classification change that starts or stops counting. Income and contribution refunds do not change `spent_amount`. The value is quantized to 0.01 and clamped at 0, including negative zero, because the column is non-negative. An edit on the same budget clamps after the net change, so it does not bounce off zero and then add the new amount. `fund_balance` is not affected by spent tracking. Update reverses the previous fund snapshot, then applies the new one, including a move between funds and to or from no fund.
- **Overspending**: Use `budget_transfer` type to move money from "Short Term Savings" sinking fund to a budget category's `fund_balance`.
- **Month-end Budget Reconciliation**: During `process_income_allocation()`, the prior month's net budget surplus/shortfall is reconciled against the configured overflow sinking fund:
    - **Net positive** (unspent money): creates a `contribution`/`transfer` transaction into the overflow fund and increments its balance.
    - **Net negative** (overspend): creates a `withdrawal`/`expense` transaction from the overflow fund and decrements its balance (can go negative, reflecting reality).
    - **Net zero**: no transaction created.
    - The net surplus formula per budget row: `allocated_amount - spent_amount + fund_balance`. All rows are summed (including negatives) before deciding whether to contribute or withdraw.
- **Budget spent repair**: `scripts/reconcile_budget_spent.py` is a one-off, dry-run unless `--apply`. For every budget it sets `spent_amount` from the ledger using `_counts_as_budget_spend` and `_write_spent` (never below zero). For each month that already has a `Budget surplus sweep` or `Budget shortfall` transaction, it posts one `Budget correction — {Month Year}` on that sweep's fund, dated today: `withdrawal`/`expense` when correct spent is higher than stored (the sweep gave the fund too much), or `contribution`/`transfer` when lower. The amount is the month's spent gap. No sweep means no overflow correction. A deleted or missing overflow fund is left unchanged. One database transaction; a second `--apply` posts nothing.
- **Recurring payments**: `app/services/recurring_payments.py`. Each bill has `paid_from` (`bills_fund` by default, `budget`, or `sinking_fund` with `paid_from_fund_id`). Only Bills-fund rows feed the forecast, the recommended amount, the cover banner, and the scheduler. Weekly and fortnightly step by 7 and 14 days. `iter_bill_due_dates` stops after `end_date`; confirm, skip, and the scheduler do too. Payments from a budget or another fund, and variable Bills-fund bills, wait in Due to confirm. Confirm is dated on the due date. A budget confirm raises `spent_amount` and, when that month was already swept and an overflow fund is set, writes a separate withdrawal that takes the amount back. Skip writes no transaction. `GET /api/bills/due`, `POST /api/bills/{id}/confirm`, and `POST /api/bills/{id}/skip` share the service with the web UI, MCP, and CLI.
- **Payment methods**: `app/services/payment_methods.py`. Table `payment_methods` (nickname, kind `card`/`bank`/`manual`, last 4, card expiry, owner, archived). `RecurringBill.payment_method_id` is optional. A card is valid through the last day of its expiry month. `PAYMENT_METHOD_EXPIRY_WARN_DAYS` (default 60, clamp 1–365) is read on each call. The dashboard banner (`id="payment-methods-banner"`, under Bills cover, any month) has one line per in-use method that is an expired or expiring card, the old side of an open replacement, or archived while a bill still uses it. Replacing a method is a saved `payment_method_replacements` job (`open`/`finished`/`stopped`) with one item per in-use bill. Ticking moves that bill immediately. Finish asks, then archives the old method only when no active bill is still on it. Stop leaves bills where they are. Nothing finishes on its own. Bills due within 7 days that are still on the old method are flagged. Web, JSON, and MCP share the service. MCP cannot delete a method or write a replacement. Nicknames, owners, and notes reject a run of 6+ digits after spaces, hyphens, and dots between digits are removed. Errors do not repeat the value.
- **Budget Overdraft Warning**: `GET /dashboard/budget-overdraft-warning?budget_id=X&amount=Y&transaction_type=...` returns an HTMX HTML fragment. Returns empty for non-`budget_expense` types, valid inputs within budget, or missing params. Used by both the Quick Expense form (dashboard) and Add Budget Transaction form (transactions page) via `hx-trigger="change"`.

## Git hooks (prek)
Hooks run automatically on `git commit` via [prek](https://prek.j178.dev/) (config: `prek.toml`). Install with `uv run prek install` (already done). Run manually with `uv run prek run --all-files`.

- **detect-secrets**: Scans staged files for secrets (API keys, passwords, tokens). `.env.example` is excluded. If a false positive is detected, update the baseline: `uv run detect-secrets scan > .secrets.baseline`. After adding a new intentional placeholder to `.env.example` or similar, regenerate the baseline the same way.
- **ruff**: Lint with auto-fix + format check.
- **pre-commit-hooks**: Merge conflict markers, large files (>500KB), EOF newlines, trailing whitespace.

## Dependency updates
Hosted [Mend Renovate](https://github.com/apps/renovate); config is `renovate.json5`. Dependabot version updates are not used; keep the GitHub dependency graph and Dependabot alerts.

- One non-major PR on the 1st and 15th (`Australia/Brisbane`). Majors are separate PRs in the same window. Nothing automerges.
- Python dependencies are exact pins (`==`) so `uv lock` cannot jump past the version Renovate selected (`rangeStrategy: "pin"`).
- uv toolchain (`aqua.yaml` `astral-sh/uv`, GHCR `ghcr.io/astral-sh/uv` image, CI `setup-uv` `version:`) is always one isolated `uv` PR (all update types, including 0.x minors and 1.x).
- Python `3.14` → `3.15` is a **minor**, isolated as `python runtime` and Dashboard-gated. Do not merge until `requires-python` and `[tool.pyrefly] python-version` move in the same commit.
- `minimumReleaseAge: "7 days"` where the registry publishes timestamps. The GitHub release of `astral-sh/uv` is the clock for the uv group (GHCR is timestamp-optional on that docker member only).
- Pending updates live on the Dependency Dashboard issue. Dashboard “Run now” starts a job; it does **not** skip the schedule for new PRs.

## Common Pitfalls
- When modifying Pydantic models or API responses, ensure all values are JSON-serializable. Specifically, convert Decimal objects to float before returning them in responses or error payloads.
- FastMCP 3.x `@mcp.tool()` returns the original function directly (no `FunctionTool` wrapper). Functions are directly callable; no `.fn` accessor needed.
- SQLite needs batch mode (`render_as_batch=True`) for ALTER TABLE operations in Alembic migrations.
- **Exception syntax**: Python 3.14 (PEP 758) allows `except ValueError, TypeError:` without parentheses. ruff-format will drop the parens. The Python 2 meaning (`except E, name`) is gone; use `except E as name` to bind. Binding a name still requires parentheses: `except (ValueError, TypeError) as exc`.
- **pyrefly and SQLAlchemy rows**: pyrefly rejects attribute access on multi-column `Row` results. Unpack tuples (`ticker, price_date = row`) instead of `row.ticker`.
- **Alpha Vantage**: never log request URLs. `httpx` and `httpcore` loggers are set to WARNING in `app/services/market_data.py`, and `redact()` strips the configured key and `apikey=` query values. Log market-data failures with `logger.error` and a redacted traceback so `exc_info` does not put the request URL back into the log.
- **PostgreSQL driver**: use `postgresql://` (psycopg v3). A `postgresql+psycopg2://` URL will fail because `psycopg2` is not installed. Dropping `psycopg[binary]` makes `alembic upgrade` crash on container start with `ModuleNotFoundError: No module named 'psycopg'`.

## CLI (`glow`)
- Entry point: `glow = "app.cli.main:app"` (defined in `[project.scripts]`).
- Package lives in `app/cli/`: `main.py` (Typer app), `config.py` (config file), `client.py` (httpx wrapper), `commands/` (one module per subcommand group).
- Config stored at `~/.config/glow-worm/config.toml` with `url` and `api_key` keys. Read via stdlib `tomllib`; written manually (no extra dep).
- Auth: reads `api_key` from config, sends `Authorization: Bearer <key>` on every request.
- `print_json()` helper in `client.py` — use for `--json` output, handles non-serialisable types via `default=str`.
- All commands accept `--json` to output raw API response instead of rich tables.
- Tests in `tests/test_cli.py`: use `typer.testing.CliRunner` + `respx` to mock httpx; patch `app.cli.client.require_config` for HTTP tests, patch `app.cli.config.CONFIG_FILE` / `CONFIG_DIR` for config file tests.
- Subcommands: `config` (set-url, set-key, show), `dashboard`, `tx` (list, add, delete), `bills` (list, pay, add, delete, due, confirm, skip), `funds` (list, add, delete), `budgets` (list, add, delete), `categories` (list, add, delete). `bills add` takes `--paid-from` and `--fund-id`.

## MCP Server
- Full MCP server implemented with **FastMCP 3.x**, mounted at `/mcp` via SSE transport. `mcp_app.lifespan` is combined with the app lifespan via `combine_lifespans` (required in v3).
- **18 tools** exposed: CRUD for transactions (`list_transactions`, `get_transaction`, `create_transaction`, `update_transaction`, `delete_transaction`), recurring bills (`list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`, `list_due_payments`, `confirm_payment`, `skip_payment`), and payment methods (`list_payment_methods`, `get_payment_method`, `create_payment_method`, `update_payment_method`, `archive_payment_method`). No payment-method delete tool and no replacement writes. `update_bill` and `update_payment_method` use `_OMIT` so null clears a link. `paid_from` on create defaults to `bills_fund`.
- `create_transaction`, `update_transaction`, and `delete_transaction` call `app/services/transaction_balances.py`, same as the transaction routes. Passing null for `sinking_fund_id` or `budget_id` on update clears that link; omitting the argument leaves it unchanged.
- Uses `contextvars.ContextVar` to propagate the authenticated user from middleware to MCP tool handlers.
- Authenticated via Bearer token (API keys), CSRF-exempt.

## Keeping Docs Current
After any feature addition or significant change, update:
- **AGENTS.md**: architecture facts, API endpoint counts, logic specifics, pitfalls
- **README.md**: Features list, project structure if new files/dirs were added

## API Routes
- Keep `/api/` prefix for JSON-returning routes.
- 52 API endpoints across: `/api/keys`, `/api/bills`, `/api/users`, `/api/dashboard`, `/api/budgets`, `/api/categories`, `/api/transactions`, `/api/sinking-funds`, `/api/income`, `/api/monthly-cost`, `/api/shares`, `/api/payment-methods`.
