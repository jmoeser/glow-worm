from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

console = Console()


def dashboard(
    month: Annotated[
        Optional[int], typer.Option("--month", "-m", help="Month (1-12)")
    ] = None,
    year: Annotated[Optional[int], typer.Option("--year", "-y", help="Year")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show budget and sinking fund summary."""
    client = get_client()
    params: dict = {}
    if month:
        params["month"] = month
    if year:
        params["year"] = year
    resp = client.get("/api/dashboard", params=params)
    raise_for_status(resp)
    data = resp.json()

    if json_output:
        print_json(data)
        return

    month_name = data.get("month_name", "")
    yr = data.get("year", "")
    console.print(f"\n[bold cyan]Dashboard — {month_name} {yr}[/bold cyan]\n")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("Income", f"[green]${data['total_income']}[/green]")
    summary.add_row("Expenses", f"[red]${data['total_expenses']}[/red]")
    summary.add_row("Net", f"${data['net']}")
    summary.add_row("Unallocated income", f"${data['unallocated_income']}")
    summary.add_row("Budget allocated", f"${data['budget_total_allocated']}")
    summary.add_row("Budget spent", f"${data['budget_total_spent']}")
    summary.add_row("Budget remaining", f"${data['budget_total_remaining']}")
    summary.add_row("Sinking funds total", f"${data['total_sinking_funds']}")
    summary.add_row(
        "[bold]Net worth[/bold]", f"[bold]${data['total_net_worth']}[/bold]"
    )
    console.print(summary)

    if data.get("sinking_funds"):
        console.print("\n[bold]Sinking Funds[/bold]")
        sf_table = Table("Name", "Balance")
        for sf in data["sinking_funds"]:
            sf_table.add_row(
                sf["name"],
                f"${sf['current_balance']}",
            )
        console.print(sf_table)

    if data.get("recent_transactions"):
        console.print("\n[bold]Recent Transactions[/bold]")
        tx_table = Table("Date", "Description", "Amount", "Type")
        for tx in data["recent_transactions"]:
            tx_table.add_row(
                tx["date"],
                tx.get("description") or "—",
                f"${tx['amount']}",
                tx["type"],
            )
        console.print(tx_table)
