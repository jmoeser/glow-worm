# Glow-worm: Project Context & Guidelines

Loaded on every coding-agent task (Claude, Grok, Cursor, and others). Feature behavior is in `docs/`. Read that file before editing the area. Do not copy it back here.

## Project overview

A single-tenant household budgeting app: Python/FastAPI, SQLAlchemy, SQLite/PostgreSQL, Alembic migrations, Jinja2 templates, Pydantic schemas, and uv for dependency management. Always use `uv run` to execute commands (for example `uv run pytest`, `uv run alembic`).

## Common Commands

- Install dependencies: `uv sync`
- Run application: `uv run uvicorn app.main:app --reload`
- Database migrations: `uv run alembic upgrade head`
- Create initial user: `uv run python scripts/create_user.py`
- Tests: `uv run pytest` · one file: `uv run pytest tests/test_filename.py` · coverage: `uv run pytest --cov=app --cov-report=html`
- Type check: `uv run pyrefly check app/` · lint: `uv run ruff check .` · format check: `uv run ruff format --check .` · format fix: `uv run ruff format .`
- Install git hooks: `uv run prek install` · run hooks: `uv run prek run --all-files`
- Update secrets baseline: `uv run detect-secrets scan > .secrets.baseline`
- Build container: `container build --tag test --file Dockerfile .`
- Run container: `container run --name test --rm -e SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))") test`
- Run CLI (dev): `uv run glow --help` · install CLI via pipx: `pipx install .`
- Repair stored budget spent (dry run; `--apply` writes): `uv run python scripts/reconcile_budget_spent.py`. Docker: `docker exec -it <container-id> python scripts/reconcile_budget_spent.py`
- Review this month's distribution (dry run; `--apply` posts the current month, including a held upgrade month): `uv run python scripts/review_monthly_processing.py`. Docker: `docker exec -it <container-id> python scripts/review_monthly_processing.py`
- Audit past settlements (read only, no `--apply`; optional `--from YYYY-MM --to YYYY-MM`): `uv run python scripts/audit_settlement_history.py`. Docker: `docker exec -it <container-id> python scripts/audit_settlement_history.py`
- Run income allocation manually (honors the upgrade-month hold; it is not the dry run): `uv run python -c "from app.tasks import process_income_allocation; process_income_allocation()"`
- Run share price and FX update manually: `uv run python -c "from app.tasks import process_share_market_data; process_share_market_data()"`

## Architecture & Money Flow

Four distinct, separated money systems, plus an informational shares ledger that does not post household transactions:

1. **Income Allocation**: Automated distribution on the 1st of the month based on `IncomeAllocation` config.
2. **Monthly Budget**: Repeating monthly categories (Groceries, etc.). Funded via income; tracks `spent_amount` vs `allocated_amount`.
3. **Sinking Funds**: Savings pots (Bills, Savings, etc.) with `current_balance`.
4. **Recurring Bills**: Managed within the "Bills" sinking fund. Each bill can name the payment method that pays it (card, bank account, or BPAY / pay myself). Only the last 4 digits are stored. A bill can also point at one household contract (the plan with a provider). Payments stamp that contract when they are recorded.
5. **Shares** (informational): US share trades, FIFO cost base, and daily prices. Stored on `share_trades`, `share_prices`, `fx_rates`, and `market_data_runs`. Not linked to `Transaction`, budgets, or sinking funds.

## Code Style & Standards

- **Backend**: Python 3.14+, FastAPI (async routes), Pydantic (validation), SQLAlchemy (ORM).
- **Frontend**: Jinja2 templates + HTMX for an SPA-like feel. Tailwind CSS via CDN.
- **Database**: SQLite (default/dev) or PostgreSQL (production), set via the `DATABASE_URL` env var. PostgreSQL uses psycopg v3 (`psycopg[binary]`), which SQLAlchemy 2.1 selects for a `postgresql://` URL. Alembic imports the engine at startup and that dialect imports `psycopg` immediately. Soft-delete Categories and SinkingFunds (`is_deleted=True`) to preserve history. System categories (`is_system=True`) cannot be deleted: income allocation needs the first `income`-type and the `transfer`-type category; bill tracking needs the `Bills` expense category.
- **Typing**: PEP 604 unions (`str | None`, `int | str`). Do **not** use `Optional[T]`, `Union[T, None]`, or `Union[...]` from `typing`. Prefer built-in generics (`list[str]`, `dict[str, int]`) over `typing.List` / `typing.Dict`.
- **Dates**: Store as ISO 8601 strings (`YYYY-MM-DD`). Use `pytz` (`Australia/Brisbane`).
- **TDD**: Write tests in `tests/` before implementation. Aim for >80% coverage.

## Middleware Stack

Outermost to innermost: CORS (optional) → Session → CSRF → Authentication. CSRF exemptions: `/login`, `/logout`, `/api/*` (Bearer token auth), `/mcp` (MCP protocol). Session: 7-day expiry (`max_age=604800`).

## Security & Safety

- **Dual authentication**. Web UI: `Starlette SessionMiddleware`; every route except `/login` requires auth. API/MCP: `Authorization: Bearer <token>`, checked before session auth. API keys are SHA-256 hashed (high-entropy tokens, not passwords), stored in `api_keys`. At most 5 active keys per user, 1 new key per 24 hours. Revoked keys do not count toward the active limit.
- **Sessions and passwords**: `User.session_version` increments on password change, invalidating all existing sessions. Passwords are hashed with `passlib` (bcrypt). Minimum 8 characters.
- **CSRF**: `starlette-csrf` is required. Every HTMX non-GET request must include `X-CSRF-Token`.
- **Transactions** support dual-linkage (`sinking_fund_id` and `recurring_bill_id` together; for example, paying a bill from a fund). Types: `regular`, `income`, `income_allocation`, `secondary_income_allocation`, `contribution`, `withdrawal`, `budget_expense`, `budget_transfer`. `secondary_income_allocation` is for goal-fund contributions from secondary income, kept distinct from `income_allocation` so monthly goal-progress tracking is not polluted by primary allocations.

## Feature detail

Read the file before changing that area. Put new behavior there, not in this file.

- Before changing bills allocation, the forecast, bills cover, or fund runway, read `docs/bills-and-forecast.md`.
- Before changing recurring transfers, the scheduler, month-end reconciliation, or `scripts/review_monthly_processing.py`, read `docs/monthly-processing.md`.
- Before changing recurring payments, read `docs/recurring-payments.md`.
- Before changing shares or market data, read `docs/shares.md`. The share price job is in the Scheduler section of `docs/monthly-processing.md`.
- Before changing contracts or payment methods, read `docs/contracts-and-payment-methods.md`.
- Before changing how transactions update balances or spent amounts, read `docs/transaction-balances.md`.
- Before changing budget funding, recommendations, overspend transfers, or the overdraft warning, read `docs/budgets.md`.
- Before changing budget-spent repair or the settlement-history audit, read `docs/scripts.md`.
- Before changing the CLI or MCP server, read `docs/cli-and-mcp.md`.

Invariants:

- `app/services/transaction_balances.py` is the only place that applies a transaction to `SinkingFund.current_balance`, `Budget.fund_balance`, and `Budget.spent_amount`. HTML routes, the JSON API, MCP transaction tools, and the dashboard quick-expense form all use it. `type` `income` or `transfer` adds the amount to the linked fund; any other `type` subtracts. `is_paid` and `transaction_type` do not change the sinking-fund balance.
- The Bills fund is not the overflow fund. Month-end surplus and shortfall use the configured overflow fund. Bills allocation stays a separate step, found by the fund name `Bills`.

## Git hooks (prek)

Hooks run on `git commit` via [prek](https://prek.j178.dev/) (`prek.toml`). Install with `uv run prek install` (already done). Run with `uv run prek run --all-files`.

- **detect-secrets**: Scans staged files for secrets (API keys, passwords, tokens). `.env.example` is excluded. On a false positive, or after adding an intentional placeholder to `.env.example` or similar, regenerate the baseline: `uv run detect-secrets scan > .secrets.baseline`.
- **ruff**: Lint with auto-fix, plus a format check.
- **pre-commit-hooks**: Merge conflict markers, large files (>500KB), EOF newlines, trailing whitespace.

## Dependency updates

Hosted [Mend Renovate](https://github.com/apps/renovate); config is `renovate.json5`. Dependabot version updates are not used; keep the GitHub dependency graph and Dependabot alerts.

- One non-major PR on the 1st and 15th (`Australia/Brisbane`). Majors are separate PRs in the same window. Nothing automerges.
- Python dependencies are exact pins (`==`) so `uv lock` cannot jump past the version Renovate selected (`rangeStrategy: "pin"`).
- The uv toolchain (`aqua.yaml` `astral-sh/uv`, GHCR `ghcr.io/astral-sh/uv` image, CI `setup-uv` `version:`) is always one isolated `uv` PR (all update types, including 0.x minors and 1.x).
- Python `3.14` → `3.15` is a **minor**, isolated as `python runtime` and Dashboard-gated. Do not merge until `requires-python` and `[tool.pyrefly] python-version` move in the same commit.
- `minimumReleaseAge: "7 days"` where the registry publishes timestamps. The GitHub release of `astral-sh/uv` is the clock for the uv group (GHCR is timestamp-optional on that docker member only).
- Pending updates live on the Dependency Dashboard issue. Dashboard “Run now” starts a job; it does **not** skip the schedule for new PRs.

## Common Pitfalls

- Pydantic models and API responses must be JSON-serializable. Convert Decimal objects to float before returning them in responses or error payloads.
- FastMCP 3.x `@mcp.tool()` returns the original function directly (no `FunctionTool` wrapper). Functions are directly callable; no `.fn` accessor is needed.
- SQLite needs batch mode (`render_as_batch=True`) for `ALTER TABLE` in Alembic migrations.
- **Exception syntax**: Python 3.14 (PEP 758) allows `except ValueError, TypeError:` without parentheses. ruff-format will drop the parens. The Python 2 meaning (`except E, name`) is gone; use `except E as name` to bind. Binding a name still requires parentheses: `except (ValueError, TypeError) as exc`.
- **pyrefly and SQLAlchemy rows**: pyrefly rejects attribute access on multi-column `Row` results. Unpack tuples (`ticker, price_date = row`) instead of `row.ticker`.
- **Alpha Vantage**: never log request URLs. `httpx` and `httpcore` loggers are set to WARNING in `app/services/market_data.py`, and `redact()` strips the configured key and `apikey=` query values. Log market-data failures with `logger.error` and a redacted traceback so `exc_info` does not put the request URL back into the log.
- **PostgreSQL driver**: use `postgresql://` (psycopg v3). A `postgresql+psycopg2://` URL will fail because `psycopg2` is not installed. Dropping `psycopg[binary]` makes `alembic upgrade` crash on container start with `ModuleNotFoundError: No module named 'psycopg'`.

## CLI and MCP

Subcommands, tool names, and tests: `docs/cli-and-mcp.md`.

- **CLI** (`glow`): entry point `glow = "app.cli.main:app"`. Config `~/.config/glow-worm/config.toml` (`url`, `api_key`), read with stdlib `tomllib`. Sends `Authorization: Bearer <key>`. Every command accepts `--json`. There is no `contracts` subcommand in v1.
- **MCP**: FastMCP 3.x, mounted at `/mcp` via SSE. `mcp_app.lifespan` is combined with the app lifespan via `combine_lifespans` (required in v3). **18 tools** (transactions, recurring bills, payment methods). No payment-method delete tool and no replacement writes. No contract tools in v1 — do not add MCP or CLI contract commands without updating this count. Bearer token, CSRF-exempt. The authenticated user is a `contextvars.ContextVar`.

## Keeping Docs Current

After any feature addition or significant change, put behavior in the matching `docs/` file. If none fits, add a topic file and one pointer under Feature detail. Do not add that detail here. Update this file only for commands, the architecture summary, style, middleware, security, hooks, dependency rules, pitfalls, the CLI/MCP summary, API conventions, or that pointer. When tools, subcommands, or the tool count change, update `docs/cli-and-mcp.md` and the summary above. Update **README.md** (features list, and project structure when new files or directories are added).

## API Routes

- Keep the `/api/` prefix for JSON-returning routes.
- 58 API endpoints across: `/api/keys`, `/api/bills`, `/api/users`, `/api/dashboard`, `/api/budgets`, `/api/categories`, `/api/transactions`, `/api/sinking-funds`, `/api/income`, `/api/monthly-cost`, `/api/shares`, `/api/contracts`, `/api/payment-methods`.
