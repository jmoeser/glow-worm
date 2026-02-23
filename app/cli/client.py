import json as _json

import httpx
import typer

from app.cli.config import require_config


def print_json(data) -> None:
    """Print data as indented JSON, serialising non-standard types via str()."""
    typer.echo(_json.dumps(data, indent=2, default=str))


def get_client() -> httpx.Client:
    config = require_config()
    return httpx.Client(
        base_url=config["url"],
        headers={"Authorization": f"Bearer {config['api_key']}"},
        timeout=10.0,
    )


def raise_for_status(response: httpx.Response) -> None:
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        typer.echo(f"Error {response.status_code}: {detail}", err=True)
        raise typer.Exit(1)
