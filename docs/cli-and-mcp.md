# CLI and MCP

Read this before changing the `glow` CLI or the MCP server. `AGENTS.md` keeps a short summary: the entry point, config, `--json`, the v1 ban on a `contracts` subcommand, the 18-tool count, and the ban on adding contract commands without updating that count.

## CLI (`glow`)

- Entry point: `glow = "app.cli.main:app"` (defined in `[project.scripts]`).
- Package lives in `app/cli/`: `main.py` (Typer app), `config.py` (config file), `client.py` (httpx wrapper), `commands/` (one module per subcommand group).
- Config stored at `~/.config/glow-worm/config.toml` with `url` and `api_key` keys. Read via stdlib `tomllib`; written manually (no extra dep).
- Auth: reads `api_key` from config, sends `Authorization: Bearer <key>` on every request.
- `print_json()` helper in `client.py` — use for `--json` output, handles non-serialisable types via `default=str`.
- All commands accept `--json` to output raw API response instead of rich tables.
- Tests in `tests/test_cli.py`: use `typer.testing.CliRunner` + `respx` to mock httpx; patch `app.cli.client.require_config` for HTTP tests, patch `app.cli.config.CONFIG_FILE` / `CONFIG_DIR` for config file tests.
- Subcommands: `config` (set-url, set-key, show), `dashboard`, `tx` (list, add, delete), `bills` (list, pay, add, delete, due, confirm, skip), `funds` (list, add, delete), `budgets` (list, add, delete), `categories` (list, add, delete). `bills add` takes `--paid-from` and `--fund-id`. `--frequency` accepts `weekly`, `fortnightly`, `28_days`, `monthly`, `quarterly`, `yearly`, and `every_2_years`. There is no `contracts` subcommand in v1.

## MCP Server

- Full MCP server implemented with **FastMCP 3.x**, mounted at `/mcp` via SSE transport. `mcp_app.lifespan` is combined with the app lifespan via `combine_lifespans` (required in v3).
- **18 tools** exposed: CRUD for transactions (`list_transactions`, `get_transaction`, `create_transaction`, `update_transaction`, `delete_transaction`), recurring bills (`list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`, `list_due_payments`, `confirm_payment`, `skip_payment`), and payment methods (`list_payment_methods`, `get_payment_method`, `create_payment_method`, `update_payment_method`, `archive_payment_method`). `create_bill` and `update_bill` document the same frequency list as the CLI, including `every_2_years`. No pay tool and no contract tools. No payment-method delete tool and no replacement writes. `update_bill` requires `expected_next_due_date` when it sends `next_due_date`. Omitting the date leaves the cursor alone. `glow tx add --bill-id` does not record a scheduled occurrence. A one-off Bills fund expense omits `--bill-id`. `glow tx delete` uses the pair-aware delete. Payment-method reads include `wallet_provider`, `funding_method_id`, and `effective_expiry_month` / `effective_expiry_year`. Creating or setting kind `wallet` is refused with "Add a wallet on the payment methods page."; any other update leaves a wallet's provider and funding card in place. No contract tools in v1 — do not add MCP or CLI contract commands without updating this count. Bill responses include `contract_id` because they share the bill serializer; the tools do not set it. `update_bill` and `update_payment_method` use `_OMIT` so null clears a link. `paid_from` on create defaults to `bills_fund`.
- `create_transaction`, `update_transaction`, and `delete_transaction` call `app/services/transaction_balances.py`, same as the transaction routes. Passing null for `sinking_fund_id` or `budget_id` on update clears that link; omitting the argument leaves it unchanged.
- Uses `contextvars.ContextVar` to propagate the authenticated user from middleware to MCP tool handlers. Auth also freezes the username, user id, and API key id and name onto that context before the middleware session closes, so later audit lines can name the key.
- Authenticated via Bearer token (API keys), CSRF-exempt.

## Audit log

Successful creates, edits, and deletes made through the JSON API (`/api/*`) or an MCP write tool append one JSON line to stdout. The `glow` CLI is covered because it calls that API with `Authorization: Bearer`. There is no audit table, no migration, and no page in the app.

HTML form routes, the scheduler, auto-pay, and every read are left out. That includes `GET /api/payment-methods/{id}` and MCP `get_payment_method`, which may commit checklist rows.

Each line is one JSON object and starts with `"event": "glow_worm.audit"`. The logger does not add an `INFO:` prefix. `PYTHONUNBUFFERED=1` is already set in the image, so the line shows up in container logs:

```bash
podman logs <container> | grep '"event": "glow_worm.audit"'
```

`source` is `rest` or `mcp`. `auth_method` is `api_key` or `session`, and a session call leaves `api_key_id` and `api_key_name` null. Ordinary writes use `create`, `update`, or `delete` (a soft delete is still `delete`). These actions keep their own names: `pay`, `confirm`, `skip`, `archive`, `unarchive`, `end`, and `revoke`.

An edit's `changes` object is `{field: {"old": ..., "new": ...}}` for mapped columns of the primary row that actually changed. `updated_at` is omitted. A create or delete carries `values`, a short redacted summary (for a transaction: id, date, amount, description, and type; other entities are the same kind of identifying fields). Side-effect rows, such as the payment written by confirm, are listed in `related` on that same line.

Money is a string of cents (`"12.50"`), never a float. Dates that are stored as `YYYY-MM-DD` stay that way. Passwords, `password_hash`, raw API keys, `key_hash`, `last4`, and email addresses are never written. A password change is recorded as `"password": {"old": "[redacted]", "new": "[redacted]"}`. If writing the line fails, the error is logged on the `app` logger and the request still succeeds.
