# Budgets

Read this before changing budget funding, recommendations, overspend transfers, or the overdraft warning. `AGENTS.md` only points here. How `fund_balance` and `spent_amount` change is in `docs/transaction-balances.md`. Month-end surplus and shortfall are in `docs/monthly-processing.md`.

## Budget funding

The "Monthly Budget Allocation" is the **sum** of all individual category `allocated_amount` targets.

## Budget recommendations

Advisory only (no auto-apply). `app/services/budget_recommendations.py` compares each current-month budget's `allocated_amount` to the **average** of `spent_amount` over up to the last **6 completed months** (min 2 months of history). Emits raise/lower suggestions only when `|delta| >= max($20, 10% of allocated)`. Shown on `/budgets` (current month panel) and as a dashboard teaser. Uses `Budget` history rows, not raw transactions.

## Overspending

Use `budget_transfer` type to move money from "Short Term Savings" sinking fund to a budget category's `fund_balance`.

## Budget overdraft warning

`GET /dashboard/budget-overdraft-warning?budget_id=X&amount=Y&transaction_type=...` returns an HTMX HTML fragment. Returns empty for non-`budget_expense` types, valid inputs within budget, or missing params. Used by both the Quick Expense form (dashboard) and Add Budget Transaction form (transactions page) via `hx-trigger="change"`.
