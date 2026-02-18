# Glow-worm: Project Context & Guidelines

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A single-tenant household budgeting app. This is a Python/FastAPI project using: SQLAlchemy + SQLite, Alembic migrations, Jinja2 templates, Pydantic schemas, uv for dependency management. Always use `uv run` to execute commands (e.g., `uv run pytest`, `uv run alembic`). When syncing dependencies, use `uv sync --extra dev` to include dev dependencies (they are under `[project.optional-dependencies]`, not `[dependency-groups]`).

## Common Commands
- **Install Dependencies**: `uv sync`
- **Run Application**: `uv run uvicorn app.main:app --reload`
- **Database Migrations**: `uv run alembic upgrade head`
- **Create Initial User**: `uv run python scripts/create_user.py`
- **Run All Tests**: `uv run pytest`
- **Run Specific Test**: `uv run pytest tests/test_filename.py`
- **Coverage Report**: `uv run pytest --cov=app --cov-report=html`
- **Build container**: `container build --tag test --file Dockerfile .`
- **Run container**: `container run --name test --rm -e SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))") test`

## Architecture & Money Flow
The app manages four distinct, separated systems:
1. **Income Allocation**: Automated distribution on the 1st of the month based on `IncomeAllocation` config.
2. **Monthly Budget**: Repeating monthly categories (Groceries, etc.). Funded via income; tracks `spent_amount` vs `allocated_amount`.
3. **Sinking Funds**: Savings pots (Bills, Savings, etc.) with `current_balance`.
4. **Recurring Bills**: Managed within the "Bills" Sinking Fund.

## Code Style & Standards
- **Backend**: Python 3.14+, FastAPI (async routes), Pydantic (validation), SQLAlchemy (ORM).
- **Frontend**: Jinja2 templates + HTMX for SPA-like feel. Tailwind CSS via CDN.
- **Database**: SQLite (dev). Use **Soft Deletes** (`is_deleted=True`) for Categories and SinkingFunds to preserve history.
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
    - Transaction types: `regular`, `income`, `income_allocation`, `contribution`, `withdrawal`, `budget_expense`, `budget_transfer`.

## Logic Specifics
- **Bills Allocation**: Recommended = (Total Annual Bill Cost / 12). Implement a "Buffer Warning" if the fund balance < 30-day upcoming bills.
- **Budget Funding**: The "Monthly Budget Allocation" is the **sum** of all individual category `allocated_amount` targets.
- **Scheduler**: Use `APScheduler`. Handle Leap Years by defaulting to the last day of the month for invalid February dates (e.g., Feb 29th -> Feb 28th).
- **Overspending**: Use `budget_transfer` type to move money from "Short Term Savings" sinking fund to a budget category's `fund_balance`.

## Common Pitfalls
- When modifying Pydantic models or API responses, ensure all values are JSON-serializable. Specifically, convert Decimal objects to float before returning them in responses or error payloads.
- FastMCP 2.x `@mcp.tool()` wraps functions into `FunctionTool` objects, not plain callables. Use `.fn` to access the underlying function for testing.
- SQLite needs batch mode (`render_as_batch=True`) for ALTER TABLE operations in Alembic migrations.

## MCP Server
- Full MCP server implemented with **FastMCP 2.x**, mounted at `/mcp` via SSE transport.
- **10 tools** exposed: CRUD for transactions (`list_transactions`, `get_transaction`, `create_transaction`, `update_transaction`, `delete_transaction`) and recurring bills (`list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`).
- Uses `contextvars.ContextVar` to propagate the authenticated user from middleware to MCP tool handlers.
- Authenticated via Bearer token (API keys), CSRF-exempt.

## API Routes
- Keep `/api/` prefix for JSON-returning routes.
- 31 API endpoints across: `/api/keys`, `/api/bills`, `/api/users`, `/api/dashboard`, `/api/budgets`, `/api/transactions`, `/api/sinking-funds`, `/api/income`.
