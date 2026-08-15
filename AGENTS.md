# Glow-worm: Project Context & Guidelines

This file provides guidance to AI coding assistants (Claude, Grok, Cursor, etc.) when working with code in this repository.

## Project overview

A single-tenant household budgeting app. This is a Python/FastAPI project using: SQLAlchemy + SQLite/PostgreSQL, Alembic migrations, Jinja2 templates, Pydantic schemas, uv for dependency management. Always use `uv run` to execute commands (e.g., `uv run pytest`, `uv run alembic`). When syncing dependencies, use `uv sync --extra dev` to include dev dependencies (they are under `[project.optional-dependencies]`, not `[dependency-groups]`).

## Common Commands
- **Install Dependencies**: `uv sync`
- **Run Application**: `uv run uvicorn app.main:app --reload`
- **Database Migrations**: `uv run alembic upgrade head`
- **Create Initial User**: `uv run python scripts/create_user.py`
- **Run All Tests**: `uv run pytest`
- **Run Specific Test**: `uv run pytest tests/test_filename.py`
- **Coverage Report**: `uv run pytest --cov=app --cov-report=html`
- **Type check**: `uv run pyrefly check app/`
- **Lint**: `uv run ruff check .`
- **Format check**: `uv run ruff format --check .`
- **Format fix**: `uv run ruff format .`
- **Update secrets baseline**: `uv run detect-secrets scan > .secrets.baseline`
- **Build container**: `container build --tag test --file Dockerfile .`
- **Run container**: `container run --name test --rm -e SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))") test`
- **Run CLI (dev)**: `uv run glow --help`
- **Install CLI via pipx**: `pipx install .`
- **Run process income allocation manually**: `uv run python -c "from app.tasks import process_income_allocation; process_income_allocation()"`

## Architecture & Money Flow
The app manages four distinct, separated systems:
1. **Income Allocation**: Automated distribution on the 1st of the month based on `IncomeAllocation` config.
2. **Monthly Budget**: Repeating monthly categories (Groceries, etc.). Funded via income; tracks `spent_amount` vs `allocated_amount`.
3. **Sinking Funds**: Savings pots (Bills, Savings, etc.) with `current_balance`.
4. **Recurring Bills**: Managed within the "Bills" Sinking Fund.

## Code Style & Standards
- **Backend**: Python 3.14+, FastAPI (async routes), Pydantic (validation), SQLAlchemy (ORM).
- **Frontend**: Jinja2 templates + HTMX for SPA-like feel. Tailwind CSS via CDN.
- **Database**: SQLite (default/dev) or PostgreSQL (production). Set via `DATABASE_URL` env var. Use **Soft Deletes** (`is_deleted=True`) for Categories and SinkingFunds to preserve history. System categories (`is_system=True`) cannot be deleted — these are required for income allocation (the first `income`-type and the `transfer`-type category) and bill tracking (the `Bills` expense category).
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
- **Bills Allocation**: Recommended = (Total Annual Bill Cost / 12). Implement a "Buffer Warning" if the fund balance < 30-day upcoming bills.
- **Budget Funding**: The "Monthly Budget Allocation" is the **sum** of all individual category `allocated_amount` targets.
- **Budget Recommendations**: Advisory only (no auto-apply). `app/services/budget_recommendations.py` compares each current-month budget's `allocated_amount` to the **average** of `spent_amount` over up to the last **6 completed months** (min 2 months of history). Emits raise/lower suggestions only when `|delta| >= max($20, 10% of allocated)`. Shown on `/budgets` (current month panel) and as a dashboard teaser. Uses `Budget` history rows, not raw transactions.
- **Recurring Transfers**: Defined on `IncomeAllocation` via the `IncomeAllocationRecurringTransfer` model. Each has a `description` and `amount`. During `process_income_allocation()`, one `expense`/`income_allocation` transaction is created per transfer (using the transfer category). The amount is deducted from `total_allocated`, reducing the unallocated remainder. No sinking fund balance is updated — the money leaves the household budget entirely. Configured via the `/income` page.
- **Scheduler**: Use `APScheduler`. Handle Leap Years by defaulting to the last day of the month for invalid February dates (e.g., Feb 29th -> Feb 28th).
- **Overspending**: Use `budget_transfer` type to move money from "Short Term Savings" sinking fund to a budget category's `fund_balance`.
- **Month-end Budget Reconciliation**: During `process_income_allocation()`, the prior month's net budget surplus/shortfall is reconciled against the configured overflow sinking fund:
    - **Net positive** (unspent money): creates a `contribution`/`transfer` transaction into the overflow fund and increments its balance.
    - **Net negative** (overspend): creates a `withdrawal`/`expense` transaction from the overflow fund and decrements its balance (can go negative, reflecting reality).
    - **Net zero**: no transaction created.
    - The net surplus formula per budget row: `allocated_amount - spent_amount + fund_balance`. All rows are summed (including negatives) before deciding whether to contribute or withdraw.
- **Budget Overdraft Warning**: `GET /dashboard/budget-overdraft-warning?budget_id=X&amount=Y&transaction_type=...` returns an HTMX HTML fragment. Returns empty for non-`budget_expense` types, valid inputs within budget, or missing params. Used by both the Quick Expense form (dashboard) and Add Budget Transaction form (transactions page) via `hx-trigger="change"`.

## Pre-commit Hooks
Hooks run automatically on `git commit`. Install with `uv run pre-commit install` (already done). Run manually with `uv run pre-commit run --all-files`.

- **detect-secrets**: Scans staged files for secrets (API keys, passwords, tokens). `.env.example` is excluded. If a false positive is detected, update the baseline: `uv run detect-secrets scan > .secrets.baseline`. After adding a new intentional placeholder to `.env.example` or similar, regenerate the baseline the same way.
- **ruff**: Lint with auto-fix + format check.
- **pre-commit-hooks**: Merge conflict markers, large files (>500KB), EOF newlines, trailing whitespace.

## Dependency updates
Hosted [Mend Renovate](https://github.com/apps/renovate); config is `renovate.json5`. Dependabot version updates are not used; keep the GitHub dependency graph and Dependabot alerts.

- One non-major PR on the 1st and 15th 00:00–06:59 (`Australia/Brisbane`). Majors are separate PRs in the same window. Nothing automerges.
- uv toolchain (`aqua.yaml` `astral-sh/uv`, GHCR `ghcr.io/astral-sh/uv` image, CI `setup-uv` `version:`) is always one isolated `uv` PR (all update types, including 0.x minors and 1.x).
- Python `3.14` → `3.15` is a **minor**, isolated as `python runtime` and Dashboard-gated. Do not merge until `requires-python` and `[tool.pyrefly] python-version` move in the same commit.
- `minimumReleaseAge: "7 days"` where the registry publishes timestamps. The GitHub release of `astral-sh/uv` is the clock for the uv group (GHCR is timestamp-optional on that docker member only).
- Pending updates live on the Dependency Dashboard issue. Dashboard “Run now” starts a job; it does **not** skip the schedule for new PRs.

## Common Pitfalls
- When modifying Pydantic models or API responses, ensure all values are JSON-serializable. Specifically, convert Decimal objects to float before returning them in responses or error payloads.
- FastMCP 3.x `@mcp.tool()` returns the original function directly (no `FunctionTool` wrapper). Functions are directly callable; no `.fn` accessor needed.
- SQLite needs batch mode (`render_as_batch=True`) for ALTER TABLE operations in Alembic migrations.
- **Exception syntax**: always use Python 3 tuple syntax — `except (ValueError, TypeError):`. Never use the Python 2 comma form `except ValueError, TypeError:` which is a `SyntaxError` in Python 3.

## CLI (`glow`)
- Entry point: `glow = "app.cli.main:app"` (defined in `[project.scripts]`).
- Package lives in `app/cli/`: `main.py` (Typer app), `config.py` (config file), `client.py` (httpx wrapper), `commands/` (one module per subcommand group).
- Config stored at `~/.config/glow-worm/config.toml` with `url` and `api_key` keys. Read via stdlib `tomllib`; written manually (no extra dep).
- Auth: reads `api_key` from config, sends `Authorization: Bearer <key>` on every request.
- `print_json()` helper in `client.py` — use for `--json` output, handles non-serialisable types via `default=str`.
- All commands accept `--json` to output raw API response instead of rich tables.
- Tests in `tests/test_cli.py`: use `typer.testing.CliRunner` + `respx` to mock httpx; patch `app.cli.client.require_config` for HTTP tests, patch `app.cli.config.CONFIG_FILE` / `CONFIG_DIR` for config file tests.
- Subcommands: `config` (set-url, set-key, show), `dashboard`, `tx` (list, add, delete), `bills` (list, pay, add, delete), `funds` (list, add, delete), `budgets` (list, add, delete), `categories` (list, add, delete).

## MCP Server
- Full MCP server implemented with **FastMCP 3.x**, mounted at `/mcp` via SSE transport. `mcp_app.lifespan` is combined with the app lifespan via `combine_lifespans` (required in v3).
- **10 tools** exposed: CRUD for transactions (`list_transactions`, `get_transaction`, `create_transaction`, `update_transaction`, `delete_transaction`) and recurring bills (`list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`).
- Uses `contextvars.ContextVar` to propagate the authenticated user from middleware to MCP tool handlers.
- Authenticated via Bearer token (API keys), CSRF-exempt.

## Keeping Docs Current
After any feature addition or significant change, update:
- **AGENTS.md**: architecture facts, API endpoint counts, logic specifics, pitfalls
- **README.md**: Features list, project structure if new files/dirs were added

## API Routes
- Keep `/api/` prefix for JSON-returning routes.
- 37 API endpoints across: `/api/keys`, `/api/bills`, `/api/users`, `/api/dashboard`, `/api/budgets`, `/api/categories`, `/api/transactions`, `/api/sinking-funds`, `/api/income`, `/api/monthly-cost`.
