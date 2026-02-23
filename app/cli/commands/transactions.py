from datetime import date
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

app = typer.Typer(help="Manage transactions.")
console = Console()


@app.command("list")
def list_transactions(
    month: Annotated[Optional[int], typer.Option(help="Month (1-12)")] = None,
    year: Annotated[Optional[int], typer.Option(help="Year")] = None,
    type_filter: Annotated[Optional[str], typer.Option("--type", help="Filter: income or expense")] = None,
    limit: Annotated[Optional[int], typer.Option(help="Max rows to display")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List transactions for a month."""
    client = get_client()
    params: dict = {}
    if month:
        params["month"] = month
    if year:
        params["year"] = year
    if type_filter:
        params["type_filter"] = type_filter
    resp = client.get("/api/transactions", params=params)
    raise_for_status(resp)
    rows = resp.json()
    if limit:
        rows = rows[:limit]
    if json_output:
        print_json(rows)
        return
    if not rows:
        typer.echo("No transactions found.")
        return
    table = Table("ID", "Date", "Description", "Amount", "Type", "Tx Type")
    for tx in rows:
        table.add_row(
            str(tx["id"]),
            tx["date"],
            tx.get("description") or "—",
            f"${tx['amount']}",
            tx["type"],
            tx["transaction_type"],
        )
    console.print(table)


@app.command("add")
def add_transaction(
    amount: Annotated[float, typer.Option(prompt=True, help="Amount (positive)")],
    category_id: Annotated[int, typer.Option(prompt=True, help="Category ID")],
    tx_type: Annotated[str, typer.Option("--type", prompt=True, help="income or expense")],
    description: Annotated[Optional[str], typer.Option(help="Description")] = None,
    transaction_type: Annotated[str, typer.Option(help="Transaction type (default: regular)")] = "regular",
    fund_id: Annotated[Optional[int], typer.Option(help="Sinking fund ID")] = None,
    bill_id: Annotated[Optional[int], typer.Option(help="Recurring bill ID")] = None,
    budget_id: Annotated[Optional[int], typer.Option(help="Budget ID")] = None,
    tx_date: Annotated[Optional[str], typer.Option("--date", help="Date YYYY-MM-DD (default: today)")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Add a new transaction."""
    if tx_date is None:
        tx_date = date.today().isoformat()
    payload: dict = {
        "amount": amount,
        "category_id": category_id,
        "type": tx_type,
        "transaction_type": transaction_type,
        "date": tx_date,
    }
    if description:
        payload["description"] = description
    if fund_id:
        payload["sinking_fund_id"] = fund_id
    if bill_id:
        payload["recurring_bill_id"] = bill_id
    if budget_id:
        payload["budget_id"] = budget_id
    client = get_client()
    resp = client.post("/api/transactions", json=payload)
    raise_for_status(resp)
    tx = resp.json()
    if json_output:
        print_json(tx)
        return
    typer.echo(f"Created transaction #{tx['id']} on {tx['date']}: ${tx['amount']}")


@app.command("delete")
def delete_transaction(
    txn_id: Annotated[int, typer.Argument(help="Transaction ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Delete a transaction."""
    if not yes:
        typer.confirm(f"Delete transaction #{txn_id}?", abort=True)
    client = get_client()
    resp = client.delete(f"/api/transactions/{txn_id}")
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo(f"Transaction #{txn_id} deleted.")
