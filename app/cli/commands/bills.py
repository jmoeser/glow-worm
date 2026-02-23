from datetime import date
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

app = typer.Typer(help="Manage recurring bills.")
console = Console()


@app.command("list")
def list_bills(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all active recurring bills."""
    client = get_client()
    resp = client.get("/api/bills")
    raise_for_status(resp)
    bills = resp.json()
    if json_output:
        print_json(bills)
        return
    if not bills:
        typer.echo("No bills found.")
        return
    table = Table("ID", "Name", "Provider", "Amount", "Frequency", "Next Due", "Type")
    for b in bills:
        table.add_row(
            str(b["id"]),
            b["name"],
            b["debtor_provider"],
            f"${b['amount']}",
            b["frequency"],
            b["next_due_date"],
            b["bill_type"],
        )
    console.print(table)


@app.command("pay")
def pay_bill(
    bill_id: Annotated[int, typer.Argument(help="Bill ID")],
    amount: Annotated[
        Optional[float], typer.Option(help="Override payment amount")
    ] = None,
    pay_date: Annotated[
        Optional[str],
        typer.Option("--date", help="Payment date YYYY-MM-DD (default: today)"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Record payment for a bill."""
    if pay_date is None:
        pay_date = date.today().isoformat()
    client = get_client()
    if amount is None:
        bill_resp = client.get(f"/api/bills/{bill_id}")
        raise_for_status(bill_resp)
        bill = bill_resp.json()
        amount = float(bill["amount"])
        typer.echo(f"Paying {bill['name']} — default amount ${amount}")
    typer.confirm(f"Confirm payment of ${amount:.2f} on {pay_date}?", abort=True)
    resp = client.post(
        f"/api/bills/{bill_id}/pay", json={"amount": amount, "date": pay_date}
    )
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo("Payment recorded.")


@app.command("add")
def add_bill(
    name: Annotated[str, typer.Option(prompt=True)],
    amount: Annotated[float, typer.Option(prompt=True)],
    provider: Annotated[
        str, typer.Option("--provider", prompt=True, help="Debtor/provider name")
    ],
    frequency: Annotated[
        str, typer.Option(prompt=True, help="28_days / monthly / quarterly / yearly")
    ],
    category_id: Annotated[int, typer.Option(prompt=True)],
    start_date: Annotated[str, typer.Option(prompt=True, help="YYYY-MM-DD")],
    next_due_date: Annotated[str, typer.Option(prompt=True, help="YYYY-MM-DD")],
    bill_type: Annotated[str, typer.Option(help="fixed or variable")] = "fixed",
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Create a new recurring bill."""
    payload = {
        "name": name,
        "amount": amount,
        "debtor_provider": provider,
        "frequency": frequency,
        "category_id": category_id,
        "start_date": start_date,
        "next_due_date": next_due_date,
        "bill_type": bill_type,
    }
    client = get_client()
    resp = client.post("/api/bills", json=payload)
    raise_for_status(resp)
    bill = resp.json()
    if json_output:
        print_json(bill)
        return
    typer.echo(f"Created bill #{bill['id']}: {bill['name']}")


@app.command("delete")
def delete_bill(
    bill_id: Annotated[int, typer.Argument(help="Bill ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Deactivate a recurring bill."""
    if not yes:
        typer.confirm(f"Deactivate bill #{bill_id}?", abort=True)
    client = get_client()
    resp = client.delete(f"/api/bills/{bill_id}")
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo(f"Bill #{bill_id} deactivated.")
