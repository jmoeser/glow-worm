# Glow-worm: Project Context & Guidelines

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A single-tenant household budgeting app. This is a Python/FastAPI project using: SQLAlchemy + SQLite, Alembic migrations, Jinja2 templates, Pydantic schemas, uv for dependency management. Always use `uv run` to execute commands (e.g., `uv run pytest`, `uv run alembic`). When syncing dependencies, use `uv sync --dev` to include dev dependencies.

## Common Commands
- **Install Dependencies**: `uv sync`
- **Run Application**: `uv run uvicorn app.main:app --reload`
- **Database Migrations**: `uv run alembic upgrade head`
- **Create Initial User**: `uv run python scripts/create_user.py`
- **Run All Tests**: `uv run pytest`
- **Run Specific Test**: `uv run pytest tests/test_filename.py`
- **Coverage Report**: `uv run pytest --cov=app --cov-report=html`



Add under a ## Common Pitfalls section\n\nWhen modifying Pydantic models or API responses, ensure all values are JSON-serializable. Specifically, convert Decimal objects to float before returning them in responses or error payloads.

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

## Security & Safety
- **Authentication**: Session-based (`Starlette SessionMiddleware`). All routes except `/login` require auth.
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

## Future MCP Readiness
- Keep `/api/` prefix for data-returning routes.
- Ensure consistent Pydantic schema responses for eventual Model Context Protocol integration.
