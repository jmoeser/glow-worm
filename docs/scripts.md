# Repair and audit scripts

Read this before changing `scripts/reconcile_budget_spent.py` or `scripts/audit_settlement_history.py`. `AGENTS.md` only points here and keeps the command lines. `scripts/review_monthly_processing.py` is part of month-end processing; its behavior is in `docs/monthly-processing.md`.

## Settlement history audit

`scripts/audit_settlement_history.py` is read-only. For each completed month in the evidence span (or an optional `--from`/`--to`) it prints the current budget-row sum `allocated_amount - spent_amount + fund_balance`, system sweep and shortfall rows, any `household_months` settlement already stored, repair corrections, late clawbacks, and at most eight manual suggestions from a short close window. It does not bootstrap month rows, post, or repair. A missing automatic sweep is not missing money. A month with no budget rows is unknown, not zero. Manual lines are suggestions; an amount or date match is not a link. Today's overflow fund is not treated as the historical fund.

## Budget spent repair

`scripts/reconcile_budget_spent.py` is a one-off, dry-run unless `--apply`. For every budget it sets `spent_amount` from the ledger using `_counts_as_budget_spend` and `_write_spent` (never below zero). For each month that already has a `Budget surplus sweep` or `Budget shortfall` transaction, it posts one `Budget correction — {Month Year}` on that sweep's fund, dated today: `withdrawal`/`expense` when correct spent is higher than stored (the sweep gave the fund too much), or `contribution`/`transfer` when lower. The amount is the month's spent gap. No sweep means no overflow correction. A deleted or missing overflow fund is left unchanged. One database transaction; a second `--apply` posts nothing.
