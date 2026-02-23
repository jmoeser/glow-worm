from datetime import datetime
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

app = typer.Typer(help="Manage monthly budgets.")
console = Console()


@app.command("list")
def list_budgets(
    month: Annotated[Optional[int], typer.Option(help="Month (1-12)")] = None,
    year: Annotated[Optional[int], typer.Option(help="Year")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List budgets for a month."""
    client = get_client()
    params: dict = {}
    if month:
        params["month"] = month
    if year:
        params["year"] = year
    resp = client.get("/api/budgets", params=params)
    raise_for_status(resp)
    budgets = resp.json()
    if json_output:
        print_json(budgets)
        return
    if not budgets:
        typer.echo("No budgets found.")
        return
    table = Table("ID", "Category ID", "Month/Year", "Allocated", "Spent", "Remaining")
    for b in budgets:
        remaining = float(b["allocated_amount"]) - float(b["spent_amount"])
        color = "green" if remaining >= 0 else "red"
        table.add_row(
            str(b["id"]),
            str(b["category_id"]),
            f"{b['month']}/{b['year']}",
            f"${b['allocated_amount']}",
            f"${b['spent_amount']}",
            f"[{color}]${remaining:.2f}[/{color}]",
        )
    console.print(table)


@app.command("add")
def add_budget(
    category_id: Annotated[int, typer.Option(prompt=True)],
    allocated_amount: Annotated[float, typer.Option(prompt=True)],
    month: Annotated[
        Optional[int], typer.Option(help="Month (default: current)")
    ] = None,
    year: Annotated[Optional[int], typer.Option(help="Year (default: current)")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Create a new budget entry."""
    now = datetime.now()
    payload = {
        "category_id": category_id,
        "allocated_amount": allocated_amount,
        "month": month or now.month,
        "year": year or now.year,
    }
    client = get_client()
    resp = client.post("/api/budgets", json=payload)
    raise_for_status(resp)
    b = resp.json()
    if json_output:
        print_json(b)
        return
    typer.echo(
        f"Created budget #{b['id']} for category {b['category_id']} ({b['month']}/{b['year']})"
    )


@app.command("delete")
def delete_budget(
    budget_id: Annotated[int, typer.Argument(help="Budget ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Delete a budget entry."""
    if not yes:
        typer.confirm(f"Delete budget #{budget_id}?", abort=True)
    client = get_client()
    resp = client.delete(f"/api/budgets/{budget_id}")
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo(f"Budget #{budget_id} deleted.")
