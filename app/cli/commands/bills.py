from datetime import date, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status
from app.config import TIMEZONE

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
    table = Table(
        "ID",
        "Name",
        "Provider",
        "Amount",
        "Frequency",
        "Next Due",
        "Type",
        "Paid from",
    )
    for b in bills:
        table.add_row(
            str(b["id"]),
            b["name"],
            b["debtor_provider"],
            f"${b['amount']}",
            b["frequency"],
            b["next_due_date"],
            b["bill_type"],
            b.get("paid_from_label") or b.get("paid_from") or "",
        )
    console.print(table)


def _due_label(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def _source_for_prompt(bill: dict) -> str:
    label = bill.get("paid_from_label") or ""
    if label.endswith(" budget"):
        return label[: -len(" budget")]
    if label:
        return label
    paid_from = bill.get("paid_from") or "bills_fund"
    if paid_from == "bills_fund":
        return "Bills fund"
    return paid_from


@app.command("pay")
def pay_bill(
    bill_id: Annotated[int, typer.Argument(help="Bill ID")],
    amount: Annotated[
        float | None, typer.Option(help="Override payment amount")
    ] = None,
    pay_date: Annotated[
        str | None,
        typer.Option("--date", help="Payment date YYYY-MM-DD (default: today)"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Record payment for a bill."""
    if pay_date is None:
        pay_date = datetime.now(TIMEZONE).date().isoformat()
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
        str,
        typer.Option(
            prompt=True,
            help=(
                "weekly / fortnightly / 28_days / monthly / quarterly / "
                "yearly / every_2_years"
            ),
        ),
    ],
    category_id: Annotated[int, typer.Option(prompt=True)],
    start_date: Annotated[str, typer.Option(prompt=True, help="YYYY-MM-DD")],
    next_due_date: Annotated[str, typer.Option(prompt=True, help="YYYY-MM-DD")],
    bill_type: Annotated[str, typer.Option(help="fixed or variable")] = "fixed",
    paid_from: Annotated[
        str,
        typer.Option(
            "--paid-from",
            help="bills_fund, budget, or sinking_fund",
        ),
    ] = "bills_fund",
    fund_id: Annotated[
        int | None,
        typer.Option(
            "--fund-id", help="Sinking fund id when --paid-from is sinking_fund"
        ),
    ] = None,
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
        "paid_from": paid_from,
        "paid_from_fund_id": fund_id,
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


@app.command("due")
def due_bills(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List payments waiting to be confirmed or skipped."""
    client = get_client()
    resp = client.get("/api/bills/due")
    raise_for_status(resp)
    rows = resp.json()
    if json_output:
        print_json(rows)
        return
    if not rows:
        typer.echo("Nothing due to confirm.")
        return
    table = Table("ID", "Name", "Amount", "Paid from", "Due", "More")
    for row in rows:
        table.add_row(
            str(row["bill_id"]),
            row["name"],
            f"${row['expected_amount']}",
            row["paid_from_label"],
            _due_label(row["due_date"]),
            f"+{row['more_due']} more" if row["more_due"] else "",
        )
    console.print(table)


@app.command("confirm")
def confirm_bill(
    bill_id: Annotated[int, typer.Argument(help="Bill ID")],
    amount: Annotated[
        float | None, typer.Option("--amount", help="Amount to log")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Confirm the next occurrence of a payment that waits for you."""
    client = get_client()
    bill_resp = client.get(f"/api/bills/{bill_id}")
    raise_for_status(bill_resp)
    bill = bill_resp.json()
    due_date = bill["next_due_date"]
    if amount is None:
        amount = float(bill["amount"])
        amount = float(typer.prompt("Amount", default=amount))
    source = _source_for_prompt(bill)
    typer.confirm(
        f"Log ${amount:.2f} from {source} for {_due_label(due_date)}?",
        abort=True,
    )
    resp = client.post(
        f"/api/bills/{bill_id}/confirm",
        json={"amount": amount, "due_date": due_date},
    )
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    payload = resp.json()
    typer.echo(payload.get("message") or "Payment confirmed.")


@app.command("skip")
def skip_bill(
    bill_id: Annotated[int, typer.Argument(help="Bill ID")],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Skip the next occurrence. Nothing is written to the ledger."""
    client = get_client()
    bill_resp = client.get(f"/api/bills/{bill_id}")
    raise_for_status(bill_resp)
    bill = bill_resp.json()
    due_date = bill["next_due_date"]
    typer.confirm(f"Skip the {_due_label(due_date)} payment?", abort=True)
    resp = client.post(
        f"/api/bills/{bill_id}/skip",
        json={"due_date": due_date},
    )
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    payload = resp.json()
    typer.echo(payload.get("message") or "Payment skipped.")
