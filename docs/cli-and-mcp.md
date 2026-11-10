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
- **18 tools** exposed: CRUD for transactions (`list_transactions`, `get_transaction`, `create_transaction`, `update_transaction`, `delete_transaction`), recurring bills (`list_bills`, `get_bill`, `create_bill`, `update_bill`, `delete_bill`, `list_due_payments`, `confirm_payment`, `skip_payment`), and payment methods (`list_payment_methods`, `get_payment_method`, `create_payment_method`, `update_payment_method`, `archive_payment_method`). `create_bill` and `update_bill` document the same frequency list as the CLI, including `every_2_years`. No payment-method delete tool and no replacement writes. Payment-method reads include `wallet_provider`, `funding_method_id`, and `effective_expiry_month` / `effective_expiry_year`. Creating or setting kind `wallet` is refused with "Add a wallet on the payment methods page."; any other update leaves a wallet's provider and funding card in place. No contract tools in v1 — do not add MCP or CLI contract commands without updating this count. Bill responses include `contract_id` because they share the bill serializer; the tools do not set it. `update_bill` and `update_payment_method` use `_OMIT` so null clears a link. `paid_from` on create defaults to `bills_fund`.
- `create_transaction`, `update_transaction`, and `delete_transaction` call `app/services/transaction_balances.py`, same as the transaction routes. Passing null for `sinking_fund_id` or `budget_id` on update clears that link; omitting the argument leaves it unchanged.
- Uses `contextvars.ContextVar` to propagate the authenticated user from middleware to MCP tool handlers.
- Authenticated via Bearer token (API keys), CSRF-exempt.
