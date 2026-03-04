from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

app = typer.Typer(help="Manage sinking funds.")
console = Console()


@app.command("list")
def list_funds(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all sinking funds."""
    client = get_client()
    resp = client.get("/api/sinking-funds")
    raise_for_status(resp)
    funds = resp.json()
    if json_output:
        print_json(funds)
        return
    if not funds:
        typer.echo("No sinking funds found.")
        return
    table = Table("ID", "Name", "Balance", "Description")
    for f in funds:
        table.add_row(
            str(f["id"]),
            f["name"],
            f"${f['current_balance']}",
            f.get("description") or "—",
        )
    console.print(table)


@app.command("add")
def add_fund(
    name: Annotated[str, typer.Option(prompt=True)],
    color: Annotated[str, typer.Option(prompt=True, help="Hex color, e.g. #3b82f6")],
    description: Annotated[Optional[str], typer.Option(help="Description")] = None,
    balance: Annotated[float, typer.Option(help="Starting balance")] = 0.0,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Create a new sinking fund."""
    payload: dict = {
        "name": name,
        "color": color,
        "current_balance": balance,
    }
    if description:
        payload["description"] = description
    client = get_client()
    resp = client.post("/api/sinking-funds", json=payload)
    raise_for_status(resp)
    fund = resp.json()
    if json_output:
        print_json(fund)
        return
    typer.echo(f"Created sinking fund #{fund['id']}: {fund['name']}")


@app.command("delete")
def delete_fund(
    fund_id: Annotated[int, typer.Argument(help="Fund ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Soft-delete a sinking fund."""
    if not yes:
        typer.confirm(f"Delete sinking fund #{fund_id}?", abort=True)
    client = get_client()
    resp = client.delete(f"/api/sinking-funds/{fund_id}")
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo(f"Sinking fund #{fund_id} deleted.")
