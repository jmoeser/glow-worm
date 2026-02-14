# Glow Worm

An opinionated, single-tenant household budgeting app built with Python and FastAPI. Designed for one household to manage income allocation, monthly budgets, sinking funds, and recurring bills -- all from a lightweight, self-hosted web UI.

## Features

- **Income Allocation** -- Configure how your monthly income is automatically distributed across budgets, sinking funds, and bills on the 1st of each month.
- **Monthly Budgets** -- Track spending against allocated amounts for repeating categories (Groceries, Eating Out, etc.) with color-coded progress bars.
- **Sinking Funds** -- Savings pots (Bills, Savings, Emergency, etc.) that accumulate a balance over time via monthly contributions.
- **Recurring Bills** -- Manage bills with flexible frequencies (monthly, quarterly, yearly, every 28 days). Bills are automatically generated as unpaid transactions when due.
- **Transaction Ledger** -- Full transaction history with filters by date, category, and fund. Supports dual-linkage so a single transaction can reference both a sinking fund and a recurring bill.
- **Dashboard** -- At-a-glance view of your budget status, upcoming bills, fund balances, and unallocated income.
- **MCP Server** -- Built-in [Model Context Protocol](https://modelcontextprotocol.io/) server so AI agents can manage your transactions and bills programmatically.
- **API Keys** -- Generate Bearer tokens for API and MCP access, managed from the web UI.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.14+, FastAPI, Pydantic |
| Database | SQLAlchemy + SQLite |
| Migrations | Alembic |
| Frontend | Jinja2 templates, HTMX, Tailwind CSS (CDN) |
| Scheduling | APScheduler |
| Auth | Session-based (web UI), Bearer tokens (API/MCP) |
| Package Manager | [uv](https://docs.astral.sh/uv/) |

## Getting Started

### Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager

### Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/jmoeser/glow-worm.git
   cd glow-worm
   ```

2. **Install dependencies:**

   ```bash
   uv sync
   ```

3. **Configure environment variables:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set a strong `SECRET_KEY` (minimum 32 characters). Generate one with:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. **Run database migrations:**

   ```bash
   uv run alembic upgrade head
   ```

5. **Create your user account:**

   ```bash
   uv run python scripts/create_user.py
   ```

6. **Start the server:**

   ```bash
   uv run uvicorn app.main:app --reload
   ```

   Open [http://localhost:8000](http://localhost:8000) and log in.

### Seed Data (Optional)

To populate the database with sample categories, funds, bills, and transactions:

```bash
uv run python scripts/seed_data.py
```

## Docker

Build and run with Docker:

```bash
docker build -t glow-worm .
docker run -p 8000:8000 \
  -v glow-worm-data:/data \
  -e SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  glow-worm
```

The SQLite database is stored in the `/data` volume so it persists across container restarts.

To create your user account inside the container:

```bash
docker exec -it <container-id> python scripts/create_user.py
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLite connection string | `sqlite:///./glow-worm.db` |
| `SECRET_KEY` | Session signing & CSRF tokens (required, min 32 chars) | -- |
| `SECURE_COOKIES` | Set to `false` for local HTTP development | `true` |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins (leave empty to disable) | -- |
| `TIMEZONE` | Timezone for date calculations and scheduling | `Australia/Brisbane` |

## MCP Server

Glow-worm exposes an [MCP](https://modelcontextprotocol.io/) server at `/mcp` using SSE transport. This allows LLMs and AI agents to manage transactions and recurring bills programmatically.

### Available Tools

| Tool | Description |
|------|-------------|
| `list_transactions` | List transactions for a given month/year with optional filters |
| `get_transaction` | Get a single transaction by ID |
| `create_transaction` | Create a new income or expense transaction |
| `update_transaction` | Update fields on an existing transaction |
| `delete_transaction` | Delete a transaction permanently |
| `list_bills` | List recurring bills (optionally include inactive) |
| `get_bill` | Get a single recurring bill by ID |
| `create_bill` | Create a new recurring bill |
| `update_bill` | Update fields on an existing bill |
| `delete_bill` | Deactivate a recurring bill (soft delete) |

### API Authentication

MCP and API endpoints use Bearer token authentication via API keys. You can manage keys from the web UI at `/api-keys`, or via the API:

- `POST /api/keys` -- Generate a new API key (max 5 active, 1 per day)
- `GET /api/keys` -- List all your API keys
- `DELETE /api/keys/{id}` -- Revoke an API key

The plaintext key is shown only once when created. Store it securely.

### Connecting an MCP Client

Configure your MCP client to connect to:

```
URL: http://localhost:8000/mcp/sse
Transport: SSE
Headers:
  Authorization: Bearer <your-api-key>
```

Example Claude Desktop configuration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "glow-worm": {
      "url": "http://localhost:8000/mcp/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer <your-api-key>"
      }
    }
  }
}
```

## Development

### Running Tests

```bash
uv sync --extra dev
uv run pytest
```

Run a specific test file:

```bash
uv run pytest tests/test_transactions.py
```

Generate a coverage report:

```bash
uv run pytest --cov=app --cov-report=html
```

### Project Structure

```
glow-worm/
├── app/
│   ├── main.py            # FastAPI app, middleware, scheduler
│   ├── models.py          # SQLAlchemy models
│   ├── schemas.py         # Pydantic schemas & enums
│   ├── auth.py            # Password hashing & verification
│   ├── database.py        # DB engine & session
│   ├── middleware.py       # Auth middleware
│   ├── mcp_server.py      # MCP tool definitions
│   ├── templating.py      # Jinja2 template helpers
│   ├── routes/            # Route modules (auth, dashboard, bills, etc.)
│   ├── templates/         # Jinja2 HTML templates
│   └── static/            # CSS, logo, static assets
├── alembic/               # Database migrations
├── scripts/               # CLI utilities (create_user, seed_data)
├── tests/                 # Test suite
├── pyproject.toml         # Project metadata & dependencies
└── alembic.ini            # Alembic configuration
```

## License

This project is not currently published under a license. All rights reserved.
