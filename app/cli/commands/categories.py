from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from app.cli.client import get_client, print_json, raise_for_status

app = typer.Typer(help="Manage transaction categories.")
console = Console()


@app.command("list")
def list_categories(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all categories."""
    client = get_client()
    resp = client.get("/api/categories")
    raise_for_status(resp)
    categories = resp.json()
    if json_output:
        print_json(categories)
        return
    if not categories:
        typer.echo("No categories found.")
        return
    table = Table("ID", "Name", "Type", "Color", "Budget", "System")
    for c in categories:
        table.add_row(
            str(c["id"]),
            c["name"],
            c["type"],
            c["color"],
            "Yes" if c["is_budget_category"] else "No",
            "Yes" if c["is_system"] else "No",
        )
    console.print(table)


@app.command("add")
def add_category(
    name: Annotated[str, typer.Option(prompt=True)],
    type: Annotated[
        str, typer.Option(prompt=True, help="Type: income, expense, or transfer")
    ],
    color: Annotated[str, typer.Option(prompt=True, help="Hex color, e.g. #3b82f6")],
    is_budget_category: Annotated[
        bool, typer.Option(help="Mark as a budget category")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Create a new category."""
    payload = {
        "name": name,
        "type": type,
        "color": color,
        "is_budget_category": is_budget_category,
    }
    client = get_client()
    resp = client.post("/api/categories", json=payload)
    raise_for_status(resp)
    category = resp.json()
    if json_output:
        print_json(category)
        return
    typer.echo(
        f"Created category #{category['id']}: {category['name']} ({category['type']})"
    )


@app.command("delete")
def delete_category(
    category_id: Annotated[int, typer.Argument(help="Category ID")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Soft-delete a category."""
    if not yes:
        typer.confirm(f"Delete category #{category_id}?", abort=True)
    client = get_client()
    resp = client.delete(f"/api/categories/{category_id}")
    raise_for_status(resp)
    if json_output:
        print_json(resp.json())
        return
    typer.echo(f"Category #{category_id} deleted.")
