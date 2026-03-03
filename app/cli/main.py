from typing import Annotated, Optional

import typer

from app.cli.commands import bills, budgets, categories, config, funds, transactions
from app.cli.commands.dashboard import dashboard

app = typer.Typer(
    name="glow",
    help="Glow-worm CLI — household budgeting at your fingertips.",
    no_args_is_help=True,
)

app.add_typer(config.app, name="config")
app.add_typer(transactions.app, name="tx")
app.add_typer(bills.app, name="bills")
app.add_typer(funds.app, name="funds")
app.add_typer(budgets.app, name="budgets")
app.add_typer(categories.app, name="categories")
app.command("dashboard")(dashboard)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo("glow-worm 0.1.0")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            "-v",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    pass
